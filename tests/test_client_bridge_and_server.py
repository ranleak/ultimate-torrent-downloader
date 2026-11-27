"""Tests for the local HTTP client, GUI, WebSocket bridge, and parallel Range transfers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets

from utd.client.config import ClientConfig, load_config, save_config
from utd.client.launcher import ensure_console_streams
from utd.client.server import create_client_app
from utd.cloud.aria2_engine import find_free_tcp_port
from utd.cloud.backend import LocalTorrentBackend
from utd.cloud.server import create_cloud_app
from utd.shared.bencode import bencode


def test_config_roundtrip_and_console_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "cfg" / "config.json"
    cfg = ClientConfig(
        modal_endpoint_url="https://demo--utd.modal.run/",
        api_token="tok-1234567890",
        local_download_dir=str(tmp_path / "dl"),
        transfer_concurrency=6,
    )
    save_config(cfg, cfg_path)
    loaded = load_config(cfg_path)
    assert loaded.modal_endpoint_url == "https://demo--utd.modal.run/"
    assert loaded.normalized_endpoint() == "https://demo--utd.modal.run"
    assert loaded.api_token == "tok-1234567890"
    assert loaded.transfer_concurrency == 6

    # Simulate pythonw.exe where stdout/stderr are None
    monkeypatch.setenv("UTD_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log_path = ensure_console_streams()
    assert log_path is not None and log_path.parent.is_dir()
    assert sys.stdout is not None and sys.stderr is not None


@pytest.mark.asyncio
async def test_client_bridge_gui_ws_and_parallel_range_transfer(tmp_path: Path) -> None:
    cloud_root = tmp_path / "modal-vol"
    downloads_dir = cloud_root / "downloads" / "pack"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    # Create a >3 MiB deterministic binary file to verify multi-chunk parallel Range reassembly
    payload = os.urandom(3 * 1024 * 1024 + 12345)
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    (downloads_dir / "bigfile.bin").write_bytes(payload)

    cloud_backend = LocalTorrentBackend(cloud_root)
    cloud_app = create_cloud_app(cloud_backend, api_token="cloud-secret")
    cloud_port = find_free_tcp_port()
    cloud_server = uvicorn.Server(
        uvicorn.Config(cloud_app, host="127.0.0.1", port=cloud_port, log_level="error")
    )
    cloud_task = asyncio.create_task(cloud_server.serve())
    await asyncio.sleep(0.15)

    local_dl_dir = tmp_path / "pc-downloads"
    cfg = ClientConfig(
        modal_endpoint_url=f"http://127.0.0.1:{cloud_port}",
        api_token="cloud-secret",
        local_download_dir=str(local_dl_dir),
        transfer_concurrency=4,
        transfer_chunk_mb=1,
    )
    client_app = create_client_app(
        config_path=tmp_path / "client-cfg.json",
        initial_config=cfg,
    )
    client_port = find_free_tcp_port()
    client_server = uvicorn.Server(
        uvicorn.Config(client_app, host="127.0.0.1", port=client_port, log_level="error")
    )
    client_task = asyncio.create_task(client_server.serve())
    await asyncio.sleep(0.15)

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{client_port}") as http:
            # 1. GUI HTML & static assets served cleanly
            idx = await http.get("/")
            assert idx.status_code == 200
            assert "Ultimate Torrent Downloader" in idx.text
            assert (await http.get("/static/styles.css")).status_code == 200
            assert (await http.get("/static/app.js")).status_code == 200

            # 2. Client health & masked config (token not exposed in plain text)
            c_health = (await http.get("/api/client/health")).json()
            assert c_health["configured"] is True
            assert c_health["upstream"]["status"] == "ok"

            c_cfg = (await http.get("/api/client/config")).json()
            assert c_cfg["has_token"] is True
            assert "cloud-secret" not in json.dumps(c_cfg)

            # 3. Connection test endpoint
            t_conn = await http.post(
                "/api/client/test-connection",
                json={"modal_endpoint_url": f"http://127.0.0.1:{cloud_port}", "api_token": ""},
            )
            assert t_conn.status_code == 200
            assert t_conn.json()["ok"] is True

            # 4. Inspect .torrent locally
            sample_torrent = bencode(
                {
                    b"info": {
                        b"name": b"demo.iso",
                        b"length": 4096,
                        b"piece length": 2048,
                        b"pieces": b"0" * 20,
                    }
                }
            )
            insp = await http.post(
                "/api/client/inspect-torrent",
                files={"file": ("demo.torrent", sample_torrent, "application/x-bittorrent")},
            )
            assert insp.status_code == 200
            assert insp.json()["name"] == "demo.iso"

            # 5. Connect local WS and verify parallel Range PC transfer completes with exact SHA-256
            async with websockets.connect(f"ws://127.0.0.1:{client_port}/api/ws") as ws:
                tr_resp = await http.post(
                    "/api/transfers",
                    json={"paths": ["downloads/pack"]},
                )
                assert tr_resp.status_code == 200
                tid = tr_resp.json()[0]["transfer_id"]

                completed = False
                for _ in range(40):
                    raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    msg = json.loads(raw)
                    if msg.get("type") == "transfer" and msg["data"]["transfer_id"] == tid:
                        if msg["data"]["status"] == "completed":
                            completed = True
                            break
                        if msg["data"]["status"] == "error":
                            pytest.fail(f"Transfer failed: {msg['data']}")
                assert completed is True

            saved_file = local_dl_dir / "downloads" / "pack" / "bigfile.bin"
            assert saved_file.is_file()
            actual_sha256 = hashlib.sha256(saved_file.read_bytes()).hexdigest()
            assert actual_sha256 == expected_sha256
    finally:
        client_server.should_exit = True
        cloud_server.should_exit = True
        await client_task
        await cloud_task
        await cloud_backend.close()
