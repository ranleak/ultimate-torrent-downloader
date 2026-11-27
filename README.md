# Ultimate Torrent Downloader

High-performance, self-hostable personal torrent downloader bridging a lightweight local Windows/desktop HTTP GUI client with **Modal.com serverless fluid compute** (`aria2c` 1.37 + FastAPI + persistent **Modal Volume** storage).

## Architecture

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ Local Device (Windows / Linux / macOS)                                  │
│  utd client (FastAPI + zero-build Web GUI on http://127.0.0.1:8765)     │
│   ├─ Magnet & .torrent inspector (client-side bencode + file selection) │
│   ├─ Real-time progress & log stream (WebSocket + SSE bridge)           │
│   └─ Parallel multi-range Transfer Manager -> Local Disk                │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │ HTTPS + WSS (Bearer Token Auth)
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Modal.com Serverless Cloud                                              │
│  ├─ WebGateway (@app.cls, @modal.concurrent(max_inputs=100), @asgi_app) │
│  │   ├─ REST API (/api/jobs, /api/files, HTTP 206 Range, ZIP64 stream)  │
│  │   ├─ SSE (/api/jobs/{id}/stream) & WebSocket (/ws/jobs)              │
│  │   └─ Modal Volume SDK streaming (listdir / read_file / copy / rm)    │
│  ├─ run_torrent_job (@app.function, timeout=86400s, Modal Volume mount) │
│  │   └─ Headless aria2c (JSON-RPC 2.0, --file-allocation=none, live RPC)│
│  └─ Persistent Storage & Coordination                                   │
│      ├─ modal.Volume ("utd-downloads")                                  │
│      ├─ modal.Dict   ("utd-jobs")                                       │
│      └─ modal.Queue  ("utd-logs", "utd-control")                        │
└─────────────────────────────────────────────────────────────────────────┘
```

## Features

- **Modal 1.6+ Native Cloud Backend**:
  - Ubuntu 24.04 + Python 3.12 + `aria2` image (`modal.Image.from_registry("ubuntu:24.04", add_python="3.12")`).
  - Long-running background `.spawn()` workers (`timeout=86400s`) avoiding Modal's 150s HTTP web timeout, paired with bounded SSE streams (`<=140s`) with sequence replay and RFC 6455 WebSockets (`/ws/jobs`).
  - Direct writes to persistent `modal.Volume` with `--file-allocation=none`, periodic background `.commit()`, and immediate post-completion commits.
  - Lock-free reads from `WebGateway` via Modal's `Volume.listdir.aio` and `Volume.read_file.aio` supporting full HTTP `Range` (`206 Partial Content`) and live streaming STORED ZIP/ZIP64 folder downloads.
- **High-Speed Local Client & Explorer GUI**:
  - Single-binary-friendly FastAPI HTTP + WebSocket server serving a modern dark-mode web UI.
  - Inspect `.torrent` files locally before upload and pick individual files (`--select-file`).
  - Live log viewer + per-job speed/peer/ETA telemetry + dynamic pause/resume/rate-limit/stop-seeding controls.
  - Built-in **Volume Explorer** to browse, preview, stream `.zip` archives, rename/delete remote files, or pull files/folders to local disk using concurrent HTTP `Range` chunks.
- **Cross-Compiled Windows `.exe` Installer**:
  - Native Linux build script (`scripts/build_windows_installer.py`) pairing `uv` cross-platform `win_amd64` wheel resolution, embeddable Python 3.12, and Unicode NSIS (`makensis`) with LZMA compression.

## Quick Start

### 1. Install Dependencies

Requires [uv](https://docs.astral.sh/uv/) (`>=0.5`):

```bash
uv sync --all-extras --dev
```

### 2. Authenticate & Deploy to Modal

```bash
uv run modal setup
uv run python scripts/deploy_modal.py
```

`deploy_modal.py` automatically:
1. Generates a high-entropy `UTD_API_TOKEN` and saves it in Modal Secret `utd-auth` (pass `--rotate-token` to rotate or `--proxy-auth` to also enforce Modal Proxy Auth).
2. Deploys `src/utd/cloud/modal_app.py`.
3. Queries `WebGateway().web.get_web_url()` and writes both the URL and token into your local client configuration.

### 3. Launch the Local Client

```bash
uv run utd client
```

Your browser will open automatically at `http://127.0.0.1:8765`.

### Optional: Local Development Mode (Without Modal)

If you have `aria2c` installed locally and want to test end-to-end without deploying to Modal:

```bash
uv run utd client --local-dir ./local-volume
```

## Building the Windows Installer on Linux

Prerequisites on Linux:
- `uv`
- `nsis` (`sudo apt-get install nsis` on Debian/Ubuntu, `brew install makensis` on macOS)

Run:

```bash
./scripts/build_windows_installer.sh
```

The standalone installer is written to `dist/UltimateTorrentDownloader-<version>-win64-setup.exe` alongside its `.sha256` checksum.

### Build Options

- `--python-zip /path/to/python-3.12.10-embed-amd64.zip`: Use a pre-downloaded Windows embeddable Python ZIP (for offline or air-gapped builds).
- `--stage-only`: Stage embeddable Python, `win_amd64` wheels, `utd` client code, `.ico`, and `.nsh` manifests under `installer/windows/build/` without invoking `makensis`.

## Development & Quality Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE).
