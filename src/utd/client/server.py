"""Lightweight local FastAPI HTTP/WebSocket server serving the UTD GUI and Modal bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from utd.client.bridge import (
    LocalTransferManager,
    ModalBridgeClient,
    ModalBridgeError,
    TransferItem,
)
from utd.client.config import ClientConfig, get_config_path, load_config, save_config
from utd.shared.bencode import BencodeError, inspect_torrent_bytes
from utd.shared.models import (
    JobOptionsPatch,
    MagnetSubmitRequest,
    RenameFileRequest,
    TorrentOptions,
)
from utd.shared.version import get_package_version

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ClientConfigUpdate(BaseModel):
    modal_endpoint_url: str | None = None
    api_token: str | None = None
    local_download_dir: str | None = None
    transfer_concurrency: int | None = Field(default=None, ge=1, le=16)
    transfer_chunk_mb: int | None = Field(default=None, ge=1, le=64)
    default_options: TorrentOptions | None = None


class TestConnectionRequest(BaseModel):
    modal_endpoint_url: str
    api_token: str = ""


class StartTransferRequest(BaseModel):
    paths: list[str] = Field(min_length=1)


class OpenFolderRequest(BaseModel):
    path: str | None = None


def _mask_token(token: str) -> str:
    clean = token.strip()
    if not clean:
        return ""
    if len(clean) <= 6:
        return "*" * len(clean)
    return f"{clean[:3]}...{clean[-4:]}"


def _config_public_view(cfg: ClientConfig) -> dict[str, Any]:
    return {
        "modal_endpoint_url": cfg.modal_endpoint_url,
        "has_token": bool(cfg.api_token.strip()),
        "api_token_masked": _mask_token(cfg.api_token),
        "local_download_dir": cfg.local_download_dir,
        "transfer_concurrency": cfg.transfer_concurrency,
        "transfer_chunk_mb": cfg.transfer_chunk_mb,
        "default_options": cfg.default_options.model_dump(),
        "config_path": str(get_config_path()),
    }


def _resolve_dir_str(raw: str) -> str:
    return str(Path(raw).expanduser().resolve())


def _resolve_open_target(base_dir_str: str, requested_path: str) -> Path:
    base = Path(base_dir_str).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = base
    if requested_path:
        candidate = Path(requested_path).expanduser().resolve()
        if candidate == base or base in candidate.parents:
            target = candidate if candidate.is_dir() else candidate.parent
    if not target.exists():
        target = base
    return target


def _open_os_folder(target: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def create_client_app(
    *,
    config_path: Path | None = None,
    initial_config: ClientConfig | None = None,
    bridge_client: ModalBridgeClient | None = None,
) -> FastAPI:
    """Create the local HTTP server FastAPI app serving the GUI and Modal bridge."""
    state: dict[str, Any] = {
        "config": initial_config or load_config(config_path),
        "config_path": config_path,
    }

    def get_cfg() -> ClientConfig:
        return state["config"]

    bridge = bridge_client or ModalBridgeClient(get_cfg)
    transfers = LocalTransferManager(bridge, get_cfg)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if bridge_client is None:
            await bridge.close()

    app = FastAPI(
        title="Ultimate Torrent Downloader Client",
        version=get_package_version(),
        lifespan=lifespan,
    )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        html_file = STATIC_DIR / "index.html"
        if not html_file.is_file():
            return JSONResponse({"error": "Static GUI assets not found"}, status_code=404)
        return FileResponse(html_file, media_type="text/html; charset=utf-8")

    @app.get("/api/client/health")
    async def client_health() -> dict[str, Any]:
        cfg = get_cfg()
        upstream_health: dict[str, Any] | None = None
        upstream_error: str | None = None
        if cfg.normalized_endpoint():
            try:
                upstream_health = await bridge.request_json("GET", "/health")
            except ModalBridgeError as exc:
                upstream_error = exc.detail
        return {
            "status": "ok",
            "version": get_package_version(),
            "configured": bool(cfg.normalized_endpoint()),
            "modal_endpoint_url": cfg.normalized_endpoint(),
            "local_download_dir": cfg.local_download_dir,
            "upstream": upstream_health,
            "upstream_error": upstream_error,
        }

    @app.get("/api/client/config")
    async def get_client_config() -> dict[str, Any]:
        return _config_public_view(get_cfg())

    @app.put("/api/client/config")
    async def update_client_config(payload: ClientConfigUpdate) -> dict[str, Any]:
        cur: ClientConfig = get_cfg()
        updated = cur.model_copy(deep=True)
        if payload.modal_endpoint_url is not None:
            updated.modal_endpoint_url = payload.modal_endpoint_url.strip().rstrip("/")
        if payload.api_token is not None and payload.api_token.strip() != "":
            updated.api_token = payload.api_token.strip()
        if payload.local_download_dir is not None and payload.local_download_dir.strip():
            updated.local_download_dir = await asyncio.to_thread(
                _resolve_dir_str, payload.local_download_dir.strip()
            )
        if payload.transfer_concurrency is not None:
            updated.transfer_concurrency = payload.transfer_concurrency
        if payload.transfer_chunk_mb is not None:
            updated.transfer_chunk_mb = payload.transfer_chunk_mb
        if payload.default_options is not None:
            updated.default_options = payload.default_options
        save_config(updated, state["config_path"])
        state["config"] = updated
        return _config_public_view(updated)

    @app.post("/api/client/test-connection")
    async def test_connection(payload: TestConnectionRequest) -> dict[str, Any]:
        token = payload.api_token.strip() or get_cfg().api_token
        try:
            return await bridge.test_connection(payload.modal_endpoint_url, token)
        except ModalBridgeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/api/client/inspect-torrent")
    async def inspect_torrent(file: UploadFile = File(...)) -> dict[str, Any]:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Uploaded .torrent file is empty.")
        try:
            meta = inspect_torrent_bytes(data)
        except BencodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return meta.model_dump()

    @app.post("/api/client/open-folder")
    async def open_folder(req: OpenFolderRequest) -> dict[str, Any]:
        target = await asyncio.to_thread(
            _resolve_open_target, get_cfg().local_download_dir, req.path
        )
        with contextlib.suppress(Exception):
            _open_os_folder(target)
        return {"opened": str(target)}

    # --- Proxied Cloud Torrent APIs ---

    async def _proxy_json(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Any = None,
        data: Any = None,
    ) -> Any:
        try:
            return await bridge.request_json(
                method, path, params=params, json_body=json_body, files=files, data=data
            )
        except ModalBridgeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/api/jobs/magnet")
    async def submit_magnet(req: MagnetSubmitRequest) -> Any:
        return await _proxy_json("POST", "/api/jobs/magnet", json_body=req.model_dump())

    @app.post("/api/jobs/torrent")
    async def submit_torrent(
        file: UploadFile = File(...),
        options: str = Form(default="{}"),
    ) -> Any:
        content = await file.read()
        fname = file.filename or "upload.torrent"
        return await _proxy_json(
            "POST",
            "/api/jobs/torrent",
            files={"file": (fname, content, "application/x-bittorrent")},
            data={"options": options},
        )

    @app.get("/api/jobs")
    async def list_jobs() -> Any:
        return await _proxy_json("GET", "/api/jobs")

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> Any:
        return await _proxy_json("GET", f"/api/jobs/{job_id}")

    @app.post("/api/jobs/{job_id}/pause")
    async def pause_job(job_id: str) -> Any:
        return await _proxy_json("POST", f"/api/jobs/{job_id}/pause")

    @app.post("/api/jobs/{job_id}/resume")
    async def resume_job(job_id: str) -> Any:
        return await _proxy_json("POST", f"/api/jobs/{job_id}/resume")

    @app.post("/api/jobs/{job_id}/stop-seeding")
    async def stop_seeding(job_id: str) -> Any:
        return await _proxy_json("POST", f"/api/jobs/{job_id}/stop-seeding")

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, delete_files: bool = Query(default=False)) -> Any:
        return await _proxy_json(
            "POST", f"/api/jobs/{job_id}/cancel", params={"delete_files": str(delete_files).lower()}
        )

    @app.patch("/api/jobs/{job_id}/options")
    async def update_job_options(job_id: str, patch: JobOptionsPatch) -> Any:
        return await _proxy_json(
            "PATCH", f"/api/jobs/{job_id}/options", json_body=patch.model_dump(exclude_none=True)
        )

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str, delete_files: bool = Query(default=False)) -> Any:
        return await _proxy_json(
            "DELETE", f"/api/jobs/{job_id}", params={"delete_files": str(delete_files).lower()}
        )

    @app.get("/api/jobs/{job_id}/stream")
    async def relay_job_sse(
        job_id: str,
        since_seq: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        async def _relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in bridge.stream_sse_lines(
                    f"/api/jobs/{job_id}/stream", params={"since_seq": since_seq}
                ):
                    yield chunk
            except ModalBridgeError as exc:
                err_evt = json.dumps({"detail": exc.detail})
                yield f"event: error\ndata: {err_evt}\n\n".encode()

        return StreamingResponse(
            _relay(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- Proxied Modal Volume File Explorer APIs ---

    @app.get("/api/files")
    async def list_files(
        path: str = Query(default=""),
        recursive: bool = Query(default=False),
    ) -> Any:
        return await _proxy_json(
            "GET", "/api/files", params={"path": path, "recursive": str(recursive).lower()}
        )

    @app.get("/api/files/download")
    async def proxy_download(
        request: Request,
        path: str = Query(...),
        inline: bool = Query(default=False),
    ) -> StreamingResponse:
        extra_headers: dict[str, str] = {}
        if rng := request.headers.get("range"):
            extra_headers["Range"] = rng
        try:
            status_code, resp_headers, stream = await bridge.stream_file_request(
                "/api/files/download",
                params={"path": path, "inline": str(inline).lower()},
                extra_headers=extra_headers,
            )
        except ModalBridgeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return StreamingResponse(
            stream,
            status_code=status_code,
            headers=resp_headers,
            media_type=resp_headers.get("content-type", "application/octet-stream"),
        )

    @app.get("/api/files/archive")
    async def proxy_archive(
        paths: list[str] = Query(...),
        name: str = Query(default="archive.zip"),
    ) -> StreamingResponse:
        params: list[tuple[str, str]] = [("paths", p) for p in paths]
        params.append(("name", name))
        try:
            status_code, resp_headers, stream = await bridge.stream_file_request(
                "/api/files/archive",
                params=params,
            )
        except ModalBridgeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return StreamingResponse(
            stream,
            status_code=status_code,
            headers=resp_headers,
            media_type=resp_headers.get("content-type", "application/zip"),
        )

    @app.post("/api/files/rename")
    async def rename_file(req: RenameFileRequest) -> Any:
        return await _proxy_json("POST", "/api/files/rename", json_body=req.model_dump())

    @app.delete("/api/files")
    async def delete_file(
        path: str = Query(...),
        recursive: bool = Query(default=True),
    ) -> Any:
        return await _proxy_json(
            "DELETE", "/api/files", params={"path": path, "recursive": str(recursive).lower()}
        )

    # --- Local PC Accelerated Transfer APIs ---

    @app.get("/api/transfers", response_model=list[TransferItem])
    async def list_transfers() -> list[TransferItem]:
        return transfers.list_transfers()

    @app.post("/api/transfers", response_model=list[TransferItem])
    async def start_transfers(req: StartTransferRequest) -> list[TransferItem]:
        try:
            return await transfers.start_transfers(req.paths)
        except ModalBridgeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/transfers/{transfer_id}/cancel", response_model=TransferItem)
    async def cancel_transfer(transfer_id: str) -> TransferItem:
        try:
            return await transfers.cancel_transfer(transfer_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/transfers/{transfer_id}")
    async def delete_transfer(transfer_id: str) -> dict[str, Any]:
        if not transfers.remove_transfer(transfer_id):
            raise HTTPException(status_code=404, detail=f"Transfer '{transfer_id}' not found.")
        return {"deleted": True, "transfer_id": transfer_id}

    # --- Local WebSocket multiplexing Modal WebSocket + Local Transfers ---

    @app.websocket("/api/ws")
    async def client_websocket(ws: WebSocket, job_id: str | None = Query(default=None)) -> None:
        await ws.accept()
        transfer_q = transfers.subscribe()
        send_lock = asyncio.Lock()
        stop_evt = asyncio.Event()

        async def _safe_send(payload: dict[str, Any]) -> None:
            async with send_lock:
                await ws.send_text(json.dumps(payload))

        # Send current local transfers immediately
        for item in transfers.list_transfers():
            await _safe_send({"type": "transfer", "data": item.model_dump()})

        async def _pump_transfers() -> None:
            while not stop_evt.is_set():
                msg = await transfer_q.get()
                await _safe_send(msg)

        async def _pump_upstream() -> None:
            target_ws = f"/ws/jobs/{job_id}" if job_id else "/ws/jobs"
            while not stop_evt.is_set():
                cfg = get_cfg()
                if not cfg.normalized_endpoint():
                    await _safe_send(
                        {"type": "connection", "data": {"state": "unconfigured", "ts": time.time()}}
                    )
                    await asyncio.sleep(2.0)
                    continue

                try:
                    async with await bridge.open_upstream_ws(target_ws) as upstream:
                        await _safe_send(
                            {
                                "type": "connection",
                                "data": {"state": "connected", "ts": time.time()},
                            }
                        )
                        async for raw_msg in upstream:
                            if stop_evt.is_set():
                                break
                            async with send_lock:
                                await ws.send_text(
                                    raw_msg if isinstance(raw_msg, str) else raw_msg.decode("utf-8")
                                )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await _safe_send(
                        {
                            "type": "connection",
                            "data": {"state": "reconnecting", "error": str(exc), "ts": time.time()},
                        }
                    )
                    await asyncio.sleep(3.0)

        t_task = asyncio.create_task(_pump_transfers())
        u_task = asyncio.create_task(_pump_upstream())
        try:
            while True:
                msg = await ws.receive_text()
                if msg.strip().lower() == "ping":
                    await _safe_send({"type": "pong", "ts": time.time()})
        except WebSocketDisconnect:
            pass
        finally:
            stop_evt.set()
            transfers.unsubscribe(transfer_q)
            for task in (t_task, u_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    return app
