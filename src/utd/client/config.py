"""Cross-platform configuration and paths for the local UTD client."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from utd.shared.models import TorrentOptions


def get_config_dir() -> Path:
    """Return the platform-appropriate user configuration directory."""
    override = os.environ.get("UTD_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "UltimateTorrentDownloader"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "utd"


def get_log_dir() -> Path:
    """Return the platform-appropriate log directory (used under Windows pythonw.exe)."""
    override = os.environ.get("UTD_LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        base = Path(local_app) if local_app else Path.home() / "AppData" / "Local"
        return base / "UltimateTorrentDownloader" / "logs"
    return get_config_dir() / "logs"


def get_config_path() -> Path:
    return get_config_dir() / "config.json"


def default_local_download_dir() -> str:
    return str(Path.home() / "Downloads" / "UTD")


class ClientConfig(BaseModel):
    modal_endpoint_url: str = Field(default="")
    api_token: str = Field(default="")
    local_download_dir: str = Field(default_factory=default_local_download_dir)
    transfer_concurrency: int = Field(default=4, ge=1, le=16)
    transfer_chunk_mb: int = Field(default=8, ge=1, le=64)
    default_options: TorrentOptions = Field(default_factory=TorrentOptions)

    def normalized_endpoint(self) -> str:
        return self.modal_endpoint_url.strip().rstrip("/")


def load_config(path: Path | None = None) -> ClientConfig:
    cfg_path = path or get_config_path()
    if not cfg_path.is_file():
        return ClientConfig()
    try:
        return ClientConfig.model_validate_json(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return ClientConfig()


def save_config(config: ClientConfig, path: Path | None = None) -> Path:
    cfg_path = path or get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cfg_path.with_suffix(".json.tmp")
    tmp_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.replace(cfg_path)
    return cfg_path
