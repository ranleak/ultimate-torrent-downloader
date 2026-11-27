"""Tests for aria2c command construction, status parsing, and JSON-RPC client."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
import uvicorn
from fastapi import FastAPI, Request

from utd.cloud.aria2_engine import (
    Aria2JobSupervisor,
    Aria2RpcClient,
    Aria2RpcError,
    _can_listen_on_ipv6,
    build_aria2c_args,
    find_free_tcp_port,
    parse_aria2_status,
)
from utd.shared.models import JobCommand, JobInfo, JobStatus, TorrentOptions


def test_build_aria2c_args_speed_and_volume_flags(tmp_path: Path) -> None:
    opts = TorrentOptions(
        max_download_limit="50M",
        max_upload_limit="5M",
        max_peers=200,
        disk_cache_mb=128,
        seed_time_minutes=0,
        sequential=True,
        extra_trackers=True,
        selected_files=[1, 3],
    )
    args = build_aria2c_args(
        output_dir=tmp_path,
        rpc_port=6800,
        rpc_secret="sec123",
        options=opts,
        trackers=["https://custom.example/ann"],
        is_private=False,
    )
    assert "--file-allocation=none" in args
    assert "--show-console-readout=false" in args
    assert "--disk-cache=128M" in args
    assert "--seed-time=0" in args
    assert "--bt-prioritize-piece=head=32M,tail=4M" in args
    assert "--select-file=1,3" in args
    assert any(a.startswith("--bt-tracker=https://custom.example/ann") for a in args)
    assert "--dht-entry-point=router.bittorrent.com:6881" in args

    priv_args = build_aria2c_args(
        output_dir=tmp_path,
        rpc_port=6800,
        rpc_secret="sec123",
        options=opts,
        trackers=["https://priv.example/ann"],
        is_private=True,
    )
    assert not any(a.startswith(("--bt-tracker=", "--dht-entry-point=")) for a in priv_args)


@pytest.mark.parametrize("failure", ["addrconfig", "bind", None])
def test_ipv6_listeners_require_a_usable_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str | None
) -> None:
    # A build with IPv6 support can still run in an IPv4-only container.
    monkeypatch.setattr(socket, "has_ipv6", True)
    addr = ("::1", 0, 0, 0)
    resolve = Mock(return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 6, "", addr)])
    if failure == "addrconfig":
        resolve.side_effect = socket.gaierror("Name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    sock = MagicMock()
    if failure == "bind":
        sock.__enter__.return_value.bind.side_effect = OSError("Cannot assign requested address")
    monkeypatch.setattr(socket, "socket", Mock(return_value=sock))

    ipv6 = _can_listen_on_ipv6()
    resolve.assert_called_once_with(
        None, 0, socket.AF_INET6, socket.SOCK_STREAM, flags=socket.AI_ADDRCONFIG
    )
    assert ipv6 is (failure is None)
    args = build_aria2c_args(
        output_dir=tmp_path,
        rpc_port=6800,
        rpc_secret="secret",
        options=TorrentOptions(extra_trackers=False),
        enable_ipv6=ipv6,
    )
    if ipv6:
        assert "--disable-ipv6=true" not in args
        assert "--enable-dht6=true" in args
    else:
        assert "--disable-ipv6=true" in args
        assert "--enable-dht6=false" in args
        assert "--enable-dht6=true" not in args
    assert "--enable-dht=true" in args
    assert not any(a.startswith("--dht-entry-point=") for a in args)
    assert "--rpc-listen-all=false" in args
    assert "--enable-color=false" in args


@pytest.mark.asyncio
async def test_only_missing_dht_cache_is_informational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = JobInfo(job_id="cache-test", name="test", source_type="torrent")
    worker = Aria2JobSupervisor(
        job=job, output_dir=tmp_path, on_progress=AsyncMock(), on_log=AsyncMock(), rpc_port=6800
    )
    cache_home = tmp_path / "worker cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    missing = cache_home / "aria2" / "dht.dat"
    corrupt = missing.with_name("dht6.dat")
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"bad header")
    header = "11/27 23:06:44 [ERROR] Exception caught while loading DHT routing table from "
    detail = (
        "Exception: [DHTRoutingTableDeserializer.cc:82] errorCode=1 "
        "Failed to load DHT routing table from "
    )
    corrupt_detail = f"{detail}{corrupt}. cause:bad header"
    bind_error = "11/27 23:06:44 [ERROR] IPv4 DHT: failed to bind UDP port 6914"
    socket_error = "Exception: [SocketCore.cc:312] errorCode=1 Failed to bind a socket"
    lines = [
        f"{header}{missing}",
        f"{detail}{missing}",
        f"{header}{corrupt}",
        corrupt_detail,
        bind_error,
        socket_error,
    ]
    stream = asyncio.StreamReader()
    stream.feed_data("\n".join(lines).encode())
    stream.feed_eof()
    await worker._pump_stream(stream, "aria2c")

    assert [(event.level, event.message) for event in job.logs] == [
        ("INFO", f"No saved DHT routing table at {missing}; starting with an empty table."),
        ("ERROR", f"{header}{corrupt}"),
        ("ERROR", corrupt_detail),
        ("ERROR", bind_error),
        ("ERROR", socket_error),
    ]


def test_parse_aria2_status_transitions() -> None:
    meta_state = parse_aria2_status(
        {
            "status": "active",
            "infoHash": "AABBCC",
            "totalLength": "0",
            "completedLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "files": [
                {"index": "1", "path": "[METADATA]foo", "length": "0", "completedLength": "0"}
            ],
        },
        previous_status=JobStatus.STARTING,
        fallback_name="magnet-aabbcc",
    )
    assert meta_state["status"] == JobStatus.METADATA
    assert meta_state["files"] == []

    dl_state = parse_aria2_status(
        {
            "status": "active",
            "infoHash": "aabbcc",
            "totalLength": "1000",
            "completedLength": "400",
            "downloadSpeed": "100",
            "uploadSpeed": "20",
            "connections": "12",
            "numSeeders": "5",
            "seeder": "false",
            "bittorrent": {"info": {"name": "Fedora.iso"}},
            "files": [
                {
                    "index": "1",
                    "path": "/vol/downloads/Fedora.iso",
                    "length": "1000",
                    "completedLength": "400",
                    "selected": "true",
                }
            ],
        },
        previous_status=JobStatus.METADATA,
        fallback_name="magnet-aabbcc",
    )
    assert dl_state["status"] == JobStatus.DOWNLOADING
    assert dl_state["name"] == "Fedora.iso"
    assert dl_state["progress"] == 40.0
    assert dl_state["eta_seconds"] == 6

    seed_state = parse_aria2_status(
        {
            "status": "active",
            "totalLength": "1000",
            "completedLength": "1000",
            "seeder": "true",
            "bittorrent": {"info": {"name": "Fedora.iso"}},
        },
        previous_status=JobStatus.DOWNLOADING,
        fallback_name="Fedora.iso",
    )
    assert seed_state["status"] == JobStatus.SEEDING
    assert seed_state["progress"] == 100.0

    followed = parse_aria2_status(
        {"status": "complete", "followedBy": ["gid-payload-2"]},
        previous_status=JobStatus.METADATA,
        fallback_name="magnet",
    )
    assert followed["followed_by"] == ["gid-payload-2"]


@pytest.mark.asyncio
async def test_aria2_rpc_client_against_mock_server() -> None:
    calls: list[tuple[str, list[Any]]] = []
    rpc_app = FastAPI()

    @rpc_app.post("/jsonrpc")
    async def handle_rpc(req: Request):
        body = json.loads(await req.body())
        method = body["method"]
        params = body["params"]
        calls.append((method, params))
        if params[0] != "token:mysecret":
            return {
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": 1, "message": "Unauthorized"},
            }
        if method == "aria2.getVersion":
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"version": "1.37.0"}}
        if method == "aria2.addUri":
            return {"jsonrpc": "2.0", "id": body["id"], "result": "gid-001"}
        if method == "aria2.tellStatus":
            return {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "gid": params[1],
                    "status": "complete",
                    "totalLength": "10",
                    "completedLength": "10",
                },
            }
        if method == "aria2.badMethod":
            return {
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32601, "message": "Not found"},
            }
        return {"jsonrpc": "2.0", "id": body["id"], "result": "OK"}

    port = find_free_tcp_port()
    server = uvicorn.Server(uvicorn.Config(rpc_app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.15)

    client = Aria2RpcClient(port, "mysecret")
    try:
        ver = await client.get_version()
        assert ver["version"] == "1.37.0"
        gid = await client.add_uri("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567")
        assert gid == "gid-001"
        st = await client.tell_status(gid)
        assert st["status"] == "complete"
        assert await client.change_global_option({"max-overall-download-limit": "10M"}) == "OK"
        with pytest.raises(Aria2RpcError):
            await client.call("aria2.badMethod")
    finally:
        await client.close()
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_supervisor_cancel_transitions_cleanly_to_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    rpc_app = FastAPI()
    port = find_free_tcp_port()
    job = JobInfo(job_id="cancel-sup", name="Arch.iso", source_type="magnet")
    statuses_seen: list[JobStatus] = []

    async def on_progress(updated: JobInfo) -> None:
        statuses_seen.append(updated.status)

    supervisor = Aria2JobSupervisor(
        job=job,
        output_dir=tmp_path,
        source_uri="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Arch.iso",
        on_progress=on_progress,
        on_log=AsyncMock(),
        rpc_port=port,
        poll_interval=5.0,
    )

    @rpc_app.post("/jsonrpc")
    async def handle_rpc(req: Request):
        body = json.loads(await req.body())
        method = body["method"]
        calls.append(method)
        if method == "aria2.getVersion":
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"version": "1.37.0"}}
        if method == "aria2.addUri":
            return {"jsonrpc": "2.0", "id": body["id"], "result": "gid-arch"}
        if method == "aria2.tellStatus":
            return {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "gid": "gid-arch",
                    "status": "active",
                    "totalLength": "1000",
                    "completedLength": "250",
                    "downloadSpeed": "100",
                    "uploadSpeed": "10",
                    "bittorrent": {"info": {"name": "Arch.iso"}},
                },
            }
        return {"jsonrpc": "2.0", "id": body["id"], "result": "OK"}

    class DummyProc:
        returncode = None
        stdout = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=DummyProc()))

    server = uvicorn.Server(uvicorn.Config(rpc_app, host="127.0.0.1", port=port, log_level="error"))
    srv_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.15)

    try:
        run_task = asyncio.create_task(supervisor.run())
        for _ in range(40):
            if JobStatus.DOWNLOADING in statuses_seen:
                break
            await asyncio.sleep(0.05)
        assert JobStatus.DOWNLOADING in statuses_seen

        await supervisor.enqueue_command(JobCommand(action="cancel"))
        final_job = await asyncio.wait_for(run_task, timeout=3.0)
        assert final_job.status == JobStatus.CANCELLED
        assert final_job.error is None
        assert final_job.download_speed == 0
        assert final_job.upload_speed == 0
        assert final_job.eta_seconds is None
        assert "aria2.forceRemove" in calls
    finally:
        server.should_exit = True
        await srv_task
