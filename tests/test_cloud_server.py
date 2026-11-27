"""Integration tests for the Cloud FastAPI server, auth, SSE, WebSockets, Range & ZIP."""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets

from utd.cloud.aria2_engine import find_free_tcp_port
from utd.cloud.backend import LocalTorrentBackend
from utd.cloud.server import create_cloud_app
from utd.shared.bencode import bencode
from utd.shared.models import JobInfo, JobStatus, LogEvent, TorrentFileSpec


def _make_sample_torrent(name: str = "sample-pack") -> bytes:
    info = {
        b"name": name.encode(),
        b"piece length": 16384,
        b"pieces": b"\x00" * 20,
        b"files": [
            {b"length": 11, b"path": [b"hello.txt"]},
            {b"length": 5, b"path": [b"sub", b"data.bin"]},
        ],
    }
    return bencode({b"announce": b"https://tracker.example/ann", b"info": info})


async def _fake_worker(
    job: JobInfo,
    output_dir: Path,
    _source_uri: str | None,
    _torrent_bytes: bytes | None,
    _trackers: list[str] | None,
    _is_private: bool,
    on_progress,
    on_log,
) -> None:
    job.status = JobStatus.DOWNLOADING
    job.total_bytes = 16
    job.files = [
        TorrentFileSpec(index=1, path=f"{job.name}/hello.txt", size=11, completed=0),
        TorrentFileSpec(index=2, path=f"{job.name}/sub/data.bin", size=5, completed=0),
    ]
    await on_progress(job)

    log1 = LogEvent(
        seq=1, ts=time.time(), level="INFO", source="aria2c", message="Connecting to peers"
    )
    job.logs.append(log1)
    await on_log(log1)

    target_dir = output_dir / job.name
    (target_dir / "sub").mkdir(parents=True, exist_ok=True)
    (target_dir / "hello.txt").write_bytes(b"hello world")
    (target_dir / "sub" / "data.bin").write_bytes(b"\x01\x02\x03\x04\x05")

    await asyncio.sleep(0.05)
    job.completed_bytes = 16
    job.progress = 100.0
    job.status = JobStatus.COMPLETED
    job.files[0].completed = 11
    job.files[1].completed = 5
    log2 = LogEvent(
        seq=2, ts=time.time(), level="INFO", source="aria2c", message="Download complete"
    )
    job.logs.append(log2)
    await on_log(log2)
    await on_progress(job)


@pytest.mark.asyncio
async def test_cloud_auth_and_job_lifecycle(tmp_path: Path) -> None:
    backend = LocalTorrentBackend(tmp_path, worker_runner=_fake_worker)
    app = create_cloud_app(backend, api_token="secret-token")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Health is open, reports authenticated status
        h = await client.get("/health")
        assert h.status_code == 200
        assert h.json()["authenticated"] is False

        # Missing or wrong bearer token -> 401
        assert (await client.get("/api/jobs")).status_code == 401
        assert (
            await client.get("/api/jobs", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401

        headers = {"Authorization": "Bearer secret-token"}
        h_auth = await client.get("/health", headers=headers)
        assert h_auth.json()["authenticated"] is True

        # Submit .torrent upload
        torrent_bytes = _make_sample_torrent("test-release")
        resp = await client.post(
            "/api/jobs/torrent",
            headers=headers,
            files={"file": ("test.torrent", torrent_bytes, "application/x-bittorrent")},
            data={"options": json.dumps({"max_download_limit": "25M"})},
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        await asyncio.sleep(0.15)
        job_data = (await client.get(f"/api/jobs/{job_id}", headers=headers)).json()
        assert job_data["status"] == "completed"
        assert job_data["progress"] == 100.0

        # File Explorer listing, Range download, ZIP streaming, Rename, Delete
        files = (
            await client.get(
                "/api/files", headers=headers, params={"path": "downloads/test-release"}
            )
        ).json()
        assert [f["name"] for f in files] == ["sub", "hello.txt"]

        # Range request 206
        r_resp = await client.get(
            "/api/files/download",
            headers={**headers, "Range": "bytes=6-10"},
            params={"path": "downloads/test-release/hello.txt"},
        )
        assert r_resp.status_code == 206
        assert r_resp.headers["content-range"] == "bytes 6-10/11"
        assert r_resp.content == b"world"

        # Suffix Range request
        s_resp = await client.get(
            "/api/files/download",
            headers={**headers, "Range": "bytes=-5"},
            params={"path": "downloads/test-release/hello.txt"},
        )
        assert s_resp.status_code == 206
        assert s_resp.content == b"world"

        # Out-of-bounds Range -> 416
        bad_range = await client.get(
            "/api/files/download",
            headers={**headers, "Range": "bytes=999-1005"},
            params={"path": "downloads/test-release/hello.txt"},
        )
        assert bad_range.status_code == 416

        # Live streaming ZIP archive verification
        zip_resp = await client.get(
            "/api/files/archive",
            headers=headers,
            params={"paths": "downloads/test-release", "name": "bundle.zip"},
        )
        assert zip_resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
            assert zf.testzip() is None
            assert set(zf.namelist()) == {"test-release/hello.txt", "test-release/sub/data.bin"}
            assert zf.read("test-release/hello.txt") == b"hello world"
            assert zf.read("test-release/sub/data.bin") == b"\x01\x02\x03\x04\x05"

        # Path traversal blocked
        assert (
            await client.get("/api/files", headers=headers, params={"path": "../../etc"})
        ).status_code == 400

        # Rename & delete
        rn = await client.post(
            "/api/files/rename",
            headers=headers,
            json={
                "src_path": "downloads/test-release/hello.txt",
                "dst_path": "downloads/test-release/hello-renamed.txt",
            },
        )
        assert rn.status_code == 200
        dl = await client.delete(
            "/api/files",
            headers=headers,
            params={"path": "downloads/test-release/hello-renamed.txt"},
        )
        assert dl.status_code == 200

    await backend.close()


@pytest.mark.asyncio
async def test_cloud_websocket_and_sse_streaming(tmp_path: Path) -> None:
    backend = LocalTorrentBackend(tmp_path, worker_runner=_fake_worker)
    app = create_cloud_app(backend, api_token="ws-tok")

    port = find_free_tcp_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    srv_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.15)

    try:
        # Unauthenticated WS rejected
        with pytest.raises(websockets.exceptions.WebSocketException):
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws/jobs"):
                pass

        async with (
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client,
            websockets.connect(f"ws://127.0.0.1:{port}/ws/jobs?token=ws-tok") as ws,
        ):
            magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=LiveTest"
            r = await client.post(
                "/api/jobs/magnet",
                headers={"Authorization": "Bearer ws-tok"},
                json={"uri": magnet},
            )
            assert r.status_code == 200
            job_id = r.json()["job_id"]

            seen_logs: list[str] = []
            seen_completed = False
            for _ in range(25):
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                evt = json.loads(raw)
                if evt.get("job_id") == job_id:
                    if evt["type"] == "log":
                        seen_logs.append(evt["data"]["message"])
                    if evt["type"] == "status" and evt["data"].get("status") == "completed":
                        seen_completed = True
                        break
            assert seen_completed is True
            assert "Connecting to peers" in seen_logs

            # Verify SSE replay endpoint streams snapshot + missed logs
            sse_lines: list[str] = []
            async with client.stream(
                "GET",
                f"/api/jobs/{job_id}/stream",
                headers={"Authorization": "Bearer ws-tok"},
                params={"max_duration": "1.2"},
            ) as sse_resp:
                assert sse_resp.status_code == 200
                async for line in sse_resp.aiter_lines():
                    if line:
                        sse_lines.append(line)
            joined = "\n".join(sse_lines)
            assert "event: snapshot" in joined
            assert "event: log" in joined
            assert "Download complete" in joined
    finally:
        server.should_exit = True
        await srv_task
        await backend.close()
