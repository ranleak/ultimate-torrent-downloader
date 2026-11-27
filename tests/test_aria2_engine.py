"""Tests for aria2c command construction, status parsing, and JSON-RPC client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request

from utd.cloud.aria2_engine import (
    Aria2RpcClient,
    Aria2RpcError,
    build_aria2c_args,
    find_free_tcp_port,
    parse_aria2_status,
)
from utd.shared.models import JobStatus, TorrentOptions


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

    priv_args = build_aria2c_args(
        output_dir=tmp_path,
        rpc_port=6800,
        rpc_secret="sec123",
        options=opts,
        trackers=["https://priv.example/ann"],
        is_private=True,
    )
    assert not any(a.startswith("--bt-tracker=") for a in priv_args)


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
