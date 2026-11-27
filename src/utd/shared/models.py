"""Pydantic models and validation helpers shared across cloud backend and client."""

from __future__ import annotations

import re
import time
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, Field, field_validator

_SPEED_RE = re.compile(r"^(0|[1-9]\d*(?:\.\d+)?[KkMmGg]?)$")
_WIN_INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


class JobStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    METADATA = "metadata"
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.ERROR})


def validate_speed_limit(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return "0"
    if not _SPEED_RE.match(cleaned):
        raise ValueError(
            f"Invalid speed limit '{value}'. Use '0' for unlimited or e.g. '500K', '25M', '1G'."
        )
    return cleaned


def sanitize_volume_subpath(raw: str) -> str:
    """Normalize a user-provided relative subdirectory under `/downloads`."""
    cleaned = raw.replace("\\", "/").strip().strip("/")
    if not cleaned:
        return ""
    parts: list[str] = []
    for part in PurePosixPath(cleaned).parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            raise ValueError("Path traversal ('..') is not permitted.")
        if _WIN_INVALID_CHARS.search(part):
            raise ValueError(f"Invalid characters in folder segment '{part}'.")
        parts.append(part)
    return "/".join(parts)


def sanitize_volume_path(raw: str) -> str:
    """Normalize any path inside the Modal Volume to a safe leading-slash-free POSIX path."""
    cleaned = raw.replace("\\", "/").strip()
    if cleaned in ("", "/"):
        return ""
    parts: list[str] = []
    for part in PurePosixPath(cleaned.lstrip("/")).parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            raise ValueError("Path traversal ('..') is not permitted.")
        parts.append(part)
    return "/".join(parts)


def parse_magnet_info(uri: str) -> dict[str, Any]:
    """Extract display name, info_hash, and trackers from a magnet URI."""
    parsed = urlparse(uri.strip())
    if parsed.scheme.lower() != "magnet":
        raise ValueError("URI must start with 'magnet:?'")
    qs = parse_qs(parsed.query, keep_blank_values=False)
    xt_values = qs.get("xt", [])
    info_hash: str | None = None
    for xt in xt_values:
        lower = xt.lower()
        if lower.startswith("urn:btih:"):
            info_hash = xt[9:].strip().lower()
            break
        if lower.startswith("urn:btmh:"):
            info_hash = xt[9:].strip().lower()
            break
    if not info_hash:
        raise ValueError("Magnet URI is missing a valid 'xt=urn:btih:...' infohash.")
    dn_values = qs.get("dn", [])
    display_name = unquote(dn_values[0]).strip() if dn_values else f"magnet-{info_hash[:12]}"
    trackers = [unquote(tr).strip() for tr in qs.get("tr", []) if tr.strip()]
    return {
        "name": display_name,
        "info_hash": info_hash,
        "trackers": trackers,
    }


class TorrentOptions(BaseModel):
    destination_subdir: str = Field(
        default="",
        description="Relative subfolder inside /downloads on the Modal Volume.",
    )
    max_download_limit: str = Field(default="0", description="e.g. '0', '50M', '500K'")
    max_upload_limit: str = Field(default="0", description="e.g. '0', '10M', '500K'")
    max_peers: int = Field(default=128, ge=0, le=2000)
    max_connection_per_server: int = Field(default=16, ge=1, le=16)
    split: int = Field(default=16, ge=1, le=64)
    disk_cache_mb: int = Field(default=64, ge=8, le=512)
    seed_time_minutes: int = Field(
        default=0,
        ge=0,
        le=1440,
        description="Minutes to seed after completion (0 stops immediately to save compute cost).",
    )
    seed_ratio: float = Field(default=0.0, ge=0.0, le=50.0)
    sequential: bool = Field(
        default=False,
        description="Prioritize head and tail pieces for early media preview.",
    )
    extra_trackers: bool = Field(
        default=True,
        description=(
            "Append curated public trackers for faster peer discovery "
            "(ignored for private torrents)."
        ),
    )
    selected_files: list[int] = Field(
        default_factory=list,
        description="1-based file indices to download; empty downloads all files.",
    )
    bt_stop_timeout: int = Field(
        default=0,
        ge=0,
        le=86400,
        description="Abort torrent if download speed stays 0 for N seconds (0 disables).",
    )

    @field_validator("destination_subdir")
    @classmethod
    def _clean_subdir(cls, v: str) -> str:
        return sanitize_volume_subpath(v)

    @field_validator("max_download_limit", "max_upload_limit")
    @classmethod
    def _clean_speed(cls, v: str) -> str:
        return validate_speed_limit(v)

    @field_validator("selected_files")
    @classmethod
    def _clean_selected(cls, v: list[int]) -> list[int]:
        for idx in v:
            if idx < 1:
                raise ValueError("selected_files indices must be >= 1.")
        return sorted(set(v))


class TorrentFileSpec(BaseModel):
    index: int
    path: str
    size: int
    completed: int = 0
    selected: bool = True


class TorrentMetadata(BaseModel):
    name: str
    info_hash: str
    total_size: int
    files: list[TorrentFileSpec] = Field(default_factory=list)
    trackers: list[str] = Field(default_factory=list)
    is_private: bool = False
    comment: str | None = None


class LogEvent(BaseModel):
    seq: int
    ts: float = Field(default_factory=time.time)
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    source: str = "aria2c"
    message: str


class JobInfo(BaseModel):
    job_id: str
    name: str
    source_type: Literal["magnet", "torrent", "url"]
    info_hash: str | None = None
    status: JobStatus = JobStatus.QUEUED
    options: TorrentOptions = Field(default_factory=TorrentOptions)
    output_dir: str = "downloads"
    total_bytes: int = 0
    completed_bytes: int = 0
    upload_bytes: int = 0
    download_speed: int = 0
    upload_speed: int = 0
    progress: float = 0.0
    eta_seconds: int | None = None
    connections: int = 0
    seeders: int = 0
    files: list[TorrentFileSpec] = Field(default_factory=list)
    logs: list[LogEvent] = Field(default_factory=list)
    error: str | None = None
    modal_call_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    finished_at: float | None = None


class MagnetSubmitRequest(BaseModel):
    uri: str = Field(min_length=5)
    options: TorrentOptions = Field(default_factory=TorrentOptions)

    @field_validator("uri")
    @classmethod
    def _validate_uri(cls, v: str) -> str:
        cleaned = v.strip()
        lower = cleaned.lower()
        if lower.startswith("magnet:?"):
            parse_magnet_info(cleaned)
            return cleaned
        if lower.startswith(("http://", "https://")):
            parsed = urlparse(cleaned)
            if not parsed.netloc:
                raise ValueError("HTTP/HTTPS URL must include a valid hostname.")
            return cleaned
        raise ValueError("URI must be a magnet:? link or an http(s):// URL.")


class JobOptionsPatch(BaseModel):
    max_download_limit: str | None = None
    max_upload_limit: str | None = None
    max_peers: int | None = Field(default=None, ge=0, le=2000)

    @field_validator("max_download_limit", "max_upload_limit")
    @classmethod
    def _clean_opt_speed(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_speed_limit(v)


class JobCommand(BaseModel):
    action: Literal["pause", "resume", "cancel", "stop_seeding", "update_options"]
    delete_files: bool = False
    options: JobOptionsPatch | None = None


class StreamEvent(BaseModel):
    type: Literal["snapshot", "progress", "log", "status", "ping", "error"]
    job_id: str
    seq: int = 0
    ts: float = Field(default_factory=time.time)
    data: dict[str, Any] = Field(default_factory=dict)


class FileEntryInfo(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: int = 0
    mime_type: str | None = None


class RenameFileRequest(BaseModel):
    src_path: str
    dst_path: str


class HealthInfo(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    engine: str = "aria2c"
    volume_name: str = "utd-downloads"
    authenticated: bool = False
    active_jobs: int = 0
