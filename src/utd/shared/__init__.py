"""Shared models, bencode parser, and tracker utilities."""

from utd.shared.bencode import BencodeError, inspect_torrent_bytes
from utd.shared.models import (
    FileEntryInfo,
    HealthInfo,
    JobCommand,
    JobInfo,
    JobOptionsPatch,
    JobStatus,
    LogEvent,
    MagnetSubmitRequest,
    RenameFileRequest,
    StreamEvent,
    TorrentFileSpec,
    TorrentMetadata,
    TorrentOptions,
    parse_magnet_info,
    sanitize_volume_path,
    sanitize_volume_subpath,
)

__all__ = [
    "BencodeError",
    "FileEntryInfo",
    "HealthInfo",
    "JobCommand",
    "JobInfo",
    "JobOptionsPatch",
    "JobStatus",
    "LogEvent",
    "MagnetSubmitRequest",
    "RenameFileRequest",
    "StreamEvent",
    "TorrentFileSpec",
    "TorrentMetadata",
    "TorrentOptions",
    "inspect_torrent_bytes",
    "parse_magnet_info",
    "sanitize_volume_path",
    "sanitize_volume_subpath",
]
