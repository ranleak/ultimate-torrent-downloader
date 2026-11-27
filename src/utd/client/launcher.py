"""Console-safe Windows/cross-platform launcher for the UTD local HTTP client."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import uvicorn

from utd.client.config import ClientConfig, get_log_dir, load_config, save_config
from utd.client.server import create_client_app


def ensure_console_streams() -> Path | None:
    """Redirect None stdout/stderr (common under Windows pythonw.exe) to a rotating log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    log_dir = get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "client.log"
    stream = log_file.open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = stream  # type: ignore[assignment]
    return log_file


def configure_logging() -> None:
    log_dir = get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "client.log"
    handler = RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[handler],
    )


def is_client_running(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        resp = httpx.get(f"http://{probe_host}:{port}/api/client/health", timeout=1.0)
        return resp.status_code == 200 and resp.json().get("status") == "ok"
    except Exception:
        return False


def _open_browser_when_ready(url: str) -> None:
    for _ in range(40):
        try:
            r = httpx.get(f"{url}/api/client/health", timeout=0.5)
            if r.status_code == 200:
                webbrowser.open(url)
                return
        except Exception:
            pass
        time.sleep(0.15)


def run_client(argv: list[str] | None = None) -> int:
    ensure_console_streams()
    configure_logging()

    parser = argparse.ArgumentParser(
        prog="utd-client",
        description="Ultimate Torrent Downloader local HTTP server & GUI.",
    )
    parser.add_argument("--host", default=os.environ.get("UTD_CLIENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UTD_CLIENT_PORT", "8765")))
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not automatically open browser."
    )
    parser.add_argument(
        "--backend-url", default="", help="Override/save Modal FastAPI endpoint URL."
    )
    parser.add_argument("--token", default="", help="Override/save Modal API bearer token.")
    parser.add_argument(
        "--local-dir",
        default="",
        help="Run a local in-process backend storing files here (local testing without Modal).",
    )
    args = parser.parse_args(argv)

    browser_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    gui_url = f"http://{browser_host}:{args.port}"

    if not args.local_dir and is_client_running(args.host, args.port):
        if not args.no_browser:
            webbrowser.open(gui_url)
        return 0

    cfg: ClientConfig = load_config()
    if args.backend_url or args.token:
        if args.backend_url:
            cfg.modal_endpoint_url = args.backend_url.strip().rstrip("/")
        if args.token:
            cfg.api_token = args.token.strip()
        save_config(cfg)

    dev_server: uvicorn.Server | None = None
    dev_thread: threading.Thread | None = None
    if args.local_dir:
        from utd.cloud.backend import LocalTorrentBackend
        from utd.cloud.server import create_cloud_app

        local_backend = LocalTorrentBackend(Path(args.local_dir))
        cloud_app = create_cloud_app(local_backend, allow_anonymous=True)
        cloud_port = args.port + 1
        cloud_cfg = uvicorn.Config(
            cloud_app,
            host="127.0.0.1",
            port=cloud_port,
            log_level="warning",
            use_colors=False,
        )
        dev_server = uvicorn.Server(cloud_cfg)
        dev_thread = threading.Thread(target=dev_server.run, daemon=True)
        dev_thread.start()
        cfg.modal_endpoint_url = f"http://127.0.0.1:{cloud_port}"
        cfg.api_token = ""

    app = create_client_app(initial_config=cfg)

    if not args.no_browser:
        threading.Thread(target=_open_browser_when_ready, args=(gui_url,), daemon=True).start()

    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="info",
            use_colors=False,
        )
    finally:
        if dev_server is not None:
            dev_server.should_exit = True
        if dev_thread is not None:
            with contextlib.suppress(Exception):
                dev_thread.join(timeout=2.0)
    return 0
