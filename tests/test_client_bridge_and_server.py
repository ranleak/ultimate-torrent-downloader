"""Tests for the local HTTP client, GUI, WebSocket bridge, and parallel Range transfers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
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
from utd.shared.models import JobInfo, JobStatus

_APP_JS = Path(__file__).resolve().parents[1] / "src" / "utd" / "client" / "static" / "app.js"


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime required for GUI JS test")
@pytest.mark.asyncio
async def test_gui_cancelled_torrent_is_visually_removed(tmp_path: Path) -> None:
    async def _holding_worker(
        job: JobInfo,
        _output_dir: Path,
        _source_uri: str | None,
        _torrent_bytes: bytes | None,
        _trackers: list[str] | None,
        _is_private: bool,
        on_progress,
        _on_log,
    ) -> None:
        job.status = JobStatus.DOWNLOADING
        job.total_bytes = 2048
        job.completed_bytes = 512
        job.progress = 25.0
        job.download_speed = 1024
        job.upload_speed = 128
        await on_progress(job)
        await asyncio.sleep(60.0)

    cloud_backend = LocalTorrentBackend(tmp_path / "cloud", worker_runner=_holding_worker)
    cloud_app = create_cloud_app(cloud_backend, api_token="gui-secret")
    cloud_port = find_free_tcp_port()
    cloud_server = uvicorn.Server(
        uvicorn.Config(cloud_app, host="127.0.0.1", port=cloud_port, log_level="error")
    )
    cloud_task = asyncio.create_task(cloud_server.serve())
    await asyncio.sleep(0.15)

    cfg = ClientConfig(
        modal_endpoint_url=f"http://127.0.0.1:{cloud_port}",
        api_token="gui-secret",
        local_download_dir=str(tmp_path / "local-dl"),
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

    harness = tmp_path / "run_gui_test.mjs"
    harness.write_text(
        """
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const baseUrl = process.env.CLIENT_BASE_URL;
const appJsPath = process.env.APP_JS_PATH;

function makeClassList(initial = []) {
  const set = new Set(initial);
  return {
    add: (...names) => names.forEach((n) => set.add(n)),
    remove: (...names) => names.forEach((n) => set.delete(n)),
    contains: (n) => set.has(n),
    toggle: (n, force) => {
      if (force === undefined) {
        if (set.has(n)) { set.delete(n); return false; }
        set.add(n); return true;
      }
      if (force) set.add(n); else set.delete(n);
      return Boolean(force);
    },
  };
}

function makeElement(id = "") {
  const listeners = new Map();
  let text = "";
  let html = "";
  const children = [];
  return {
    id,
    value: id === "log-level-filter" ? "ALL" : "",
    checked: id === "opt-extra-trackers" || id === "log-autoscroll",
    disabled: false,
    isConnected: true,
    dataset: {},
    style: {},
    files: [],
    scrollTop: 0,
    scrollHeight: 0,
    className: "",
    classList: makeClassList(),
    children,
    get textContent() { return text; },
    set textContent(v) {
      text = String(v ?? "");
      html = text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    },
    get innerHTML() { return html; },
    set innerHTML(v) {
      html = String(v ?? "");
      text = html;
    },
    appendChild(child) { children.push(child); return child; },
    remove() {
      const idx = children.indexOf(this);
      if (idx >= 0) children.splice(idx, 1);
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    async dispatch(type, ev = {}) {
      const list = listeners.get(type) || [];
      for (const fn of list) {
        await fn({
          preventDefault() {},
          stopPropagation() {},
          target: { closest: () => null },
          ...ev,
        });
      }
    },
    showModal() {},
    close() {},
  };
}

const elements = new Map();
function getEl(sel) {
  const id = sel.startsWith("#") ? sel.slice(1) : sel;
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
}

const wsInstances = [];
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.OPEN;
    wsInstances.push(this);
  }
  close() { this.readyState = 3; }
}

const sseInstances = [];
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = new Map();
    sseInstances.push(this);
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  emit(type, payload) {
    for (const fn of this.listeners.get(type) || []) {
      fn({ data: JSON.stringify(payload) });
    }
  }
  close() { this.closed = true; }
}

let failNextCancel = false;
async function clientFetch(input, init = {}) {
  const url = typeof input === "string" && input.startsWith("/") ? `${baseUrl}${input}` : input;
  if (failNextCancel && String(url).endsWith("/cancel")) {
    failNextCancel = false;
    return new Response(JSON.stringify({ detail: "Simulated upstream failure" }), {
      status: 502,
      headers: { "content-type": "application/json" },
    });
  }
  return fetch(url, init);
}

const host = new URL(baseUrl).host;
const context = vm.createContext({
  console,
  URL,
  FormData,
  Number,
  String,
  Boolean,
  Array,
  Object,
  Map,
  Set,
  Math,
  Date,
  JSON,
  Error,
   encodeURIComponent,
  setTimeout: () => 1,
  clearTimeout: () => {},
  fetch: clientFetch,
  WebSocket: FakeWebSocket,
  EventSource: FakeEventSource,
  location: { protocol: "http:", host, href: `${baseUrl}/` },
  navigator: { clipboard: { writeText: async () => {} } },
  window: { prompt: () => null, confirm: () => false },
  document: {
    querySelector: (sel) => getEl(sel),
    querySelectorAll: () => [],
    createElement: () => makeElement(),
  },
});

vm.runInContext(fs.readFileSync(appJsPath, "utf8"), context);
await new Promise((r) => setTimeout(r, 150));

// 1. Submit two magnet torrents via the GUI form
const magnet1 = "magnet:?xt=urn:btih:1111111111111111111111111111111111111111&dn=Alpha.iso";
const magnet2 = "magnet:?xt=urn:btih:2222222222222222222222222222222222222222&dn=Beta.iso";
getEl("#magnet-input").value = `${magnet1}\\n${magnet2}`;
await getEl("#submit-form").dispatch("submit");

const jobsResp = await (await fetch(`${baseUrl}/api/jobs`)).json();
const alphaJob = jobsResp.find((j) => j.name === "Alpha.iso");
const betaJob = jobsResp.find((j) => j.name === "Beta.iso");
assert.ok(alphaJob && betaJob);

assert.match(getEl("#jobs-list").innerHTML, /Alpha\\.iso/);
assert.match(getEl("#jobs-list").innerHTML, /Beta\\.iso/);
assert.equal(getEl("#badge-jobs").textContent, "2");
assert.equal(getEl("#active-jobs-count").textContent, "2");
assert.equal(getEl("#inspector-title").textContent, "Beta.iso");
const betaSse = sseInstances.at(-1);
assert.ok(betaSse && !betaSse.closed);

// 2. Click Cancel on Beta.iso in #jobs-list -> visually removed immediately
const cancelBtn = {
  disabled: false,
  isConnected: false,
  dataset: { act: "cancel", id: betaJob.job_id },
};
await getEl("#jobs-list").dispatch("click", {
  target: {
    closest: (sel) => (sel === "button[data-act]" ? cancelBtn : null),
  },
});

assert.doesNotMatch(getEl("#jobs-list").innerHTML, /Beta\\.iso/);
assert.doesNotMatch(getEl("#jobs-list").innerHTML, /status-cancelled/);
assert.match(getEl("#jobs-list").innerHTML, /Alpha\\.iso/);
assert.equal(getEl("#badge-jobs").textContent, "1");
assert.equal(getEl("#active-jobs-count").textContent, "1");
assert.equal(getEl("#inspector-title").textContent, "Live Inspector");
assert.match(getEl("#inspector-files-list").innerHTML, /No job selected/);
assert.equal(betaSse.closed, true);

// 3. Stale in-flight WS progress/log events for Beta.iso must NOT re-add it
const ws = wsInstances.at(-1);
ws.onmessage({
  data: JSON.stringify({
    type: "progress",
    job_id: betaJob.job_id,
    data: { ...betaJob, status: "downloading", download_speed: 9999 },
  }),
});
ws.onmessage({
  data: JSON.stringify({
    type: "log",
    job_id: betaJob.job_id,
    data: { seq: 1, ts: 1, level: "INFO", source: "aria2c", message: "late log" },
  }),
});
assert.doesNotMatch(getEl("#jobs-list").innerHTML, /Beta\\.iso/);
assert.equal(getEl("#badge-jobs").textContent, "1");

// 4. Refreshing jobs from /api/jobs keeps cancelled torrent visually removed
await getEl("#refresh-jobs-btn").dispatch("click");
assert.doesNotMatch(getEl("#jobs-list").innerHTML, /Beta\\.iso/);
assert.match(getEl("#jobs-list").innerHTML, /Alpha\\.iso/);
assert.equal(getEl("#badge-jobs").textContent, "1");

// 5. External cancellation arriving via WebSocket removes Alpha.iso too
const extCancel = await (
  await fetch(`${baseUrl}/api/jobs/${alphaJob.job_id}/cancel`, { method: "POST" })
).json();
assert.equal(extCancel.status, "cancelled");
ws.onmessage({
  data: JSON.stringify({
    type: "status",
    job_id: alphaJob.job_id,
    data: extCancel,
  }),
});
assert.doesNotMatch(getEl("#jobs-list").innerHTML, /Alpha\\.iso/);
assert.match(getEl("#jobs-list").innerHTML, /No torrents yet/);
assert.equal(getEl("#badge-jobs").textContent, "0");
assert.equal(getEl("#active-jobs-count").textContent, "0");
assert.equal(getEl("#total-dl-speed").textContent, "0 B/s");

// 6. If cancel fails on server (502), optimistic removal rolls back cleanly
const magnet3 = "magnet:?xt=urn:btih:3333333333333333333333333333333333333333&dn=Gamma.iso";
getEl("#magnet-input").value = magnet3;
await getEl("#submit-form").dispatch("submit");
const jobsAfter = await (await fetch(`${baseUrl}/api/jobs`)).json();
const gammaJob = jobsAfter.find((j) => j.name === "Gamma.iso");
assert.ok(gammaJob);
assert.match(getEl("#jobs-list").innerHTML, /Gamma\\.iso/);

failNextCancel = true;
await getEl("#jobs-list").dispatch("click", {
  target: {
    closest: (sel) =>
      sel === "button[data-act]"
        ? { disabled: false, isConnected: false, dataset: { act: "cancel", id: gammaJob.job_id } }
        : null,
  },
});
assert.match(getEl("#jobs-list").innerHTML, /Gamma\\.iso/);
assert.equal(getEl("#badge-jobs").textContent, "1");
assert.ok(
  getEl("#toast-container").children.some((t) =>
    t.textContent.includes("Simulated upstream failure")
  )
);
""",
        encoding="utf-8",
    )

    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["node", str(harness)],
            env={
                **os.environ,
                "CLIENT_BASE_URL": f"http://127.0.0.1:{client_port}",
                "APP_JS_PATH": str(_APP_JS),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    finally:
        client_server.should_exit = True
        cloud_server.should_exit = True
        await client_task
        await cloud_task
        await cloud_backend.close()
