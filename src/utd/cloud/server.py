"""FastAPI ASGI server exposing torrent management, streaming telemetry, and Volume Explorer."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from utd.cloud.backend import CloudBackend
from utd.cloud.volume_store import collect_archive_files, stream_zip_archive
from utd.shared.bencode import BencodeError
from utd.shared.models import (
    FileEntryInfo,
    HealthInfo,
    JobCommand,
    JobInfo,
    JobOptionsPatch,
    MagnetSubmitRequest,
    RenameFileRequest,
    StreamEvent,
    TorrentOptions,
    sanitize_volume_path,
)

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_http_range(range_header: str, total_size: int) -> tuple[int, int]:
    match = _RANGE_RE.match(range_header.strip())
    if not match:
        raise ValueError("Malformed Range header")
    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        raise ValueError("Empty Range bounds")
    if not raw_start:
        suffix_len = int(raw_end)
        if suffix_len <= 0:
            raise ValueError("Suffix length must be positive")
        start = max(0, total_size - suffix_len)
        end = total_size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else total_size - 1
        end = min(end, total_size - 1)
    if total_size == 0 or start >= total_size or start > end:
        raise ValueError("Range out of bounds")
    return start, end


def _verify_token(candidate: str | None, expected: str | None, allow_anonymous: bool) -> None:
    if expected is None or expected == "":
        if allow_anonymous:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Server authentication token is not configured. "
                "Set UTD_API_TOKEN or run `utd deploy`."
            ),
        )
    if not candidate or not hmac.compare_digest(candidate, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _extract_token_from_request(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token")


def _extract_token_from_ws(ws: WebSocket) -> str | None:
    auth = ws.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ws.query_params.get("token")


def create_cloud_app(
    backend: CloudBackend,
    *,
    api_token: str | None = None,
    allow_anonymous: bool | None = None,
) -> FastAPI:
    """Build the FastAPI ASGI application for the Modal cloud container."""
    resolved_token = api_token if api_token is not None else os.environ.get("UTD_API_TOKEN")
    resolved_anon = (
        allow_anonymous
        if allow_anonymous is not None
        else os.environ.get("UTD_ALLOW_ANONYMOUS", "0") == "1"
    )

    app = FastAPI(
        title="Ultimate Torrent Downloader Cloud API",
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    async def require_auth(request: Request) -> None:
        _verify_token(_extract_token_from_request(request), resolved_token, resolved_anon)

    @app.get("/health", response_model=HealthInfo)
    async def health(request: Request) -> HealthInfo:
        info = await backend.health()
        token = _extract_token_from_request(request)
        info.authenticated = bool(
            (resolved_token and token and hmac.compare_digest(token, resolved_token))
            or (not resolved_token and resolved_anon)
        )
        return info

    @app.post("/api/jobs/magnet", response_model=JobInfo, dependencies=[Depends(require_auth)])
    async def submit_magnet(req: MagnetSubmitRequest) -> JobInfo:
        try:
            return await backend.submit_magnet(req.uri, req.options)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/jobs/torrent", response_model=JobInfo, dependencies=[Depends(require_auth)])
    async def submit_torrent(
        file: UploadFile = File(...),
        options: str = Form(default="{}"),
    ) -> JobInfo:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Uploaded .torrent file is empty.")
        try:
            parsed_options = TorrentOptions.model_validate_json(options)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        try:
            return await backend.submit_torrent(
                data, file.filename or "upload.torrent", parsed_options
            )
        except (BencodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs", response_model=list[JobInfo], dependencies=[Depends(require_auth)])
    async def list_jobs() -> list[JobInfo]:
        return await backend.list_jobs()

    @app.get("/api/jobs/{job_id}", response_model=JobInfo, dependencies=[Depends(require_auth)])
    async def get_job(job_id: str) -> JobInfo:
        job = await backend.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        return job

    @app.post(
        "/api/jobs/{job_id}/pause", response_model=JobInfo, dependencies=[Depends(require_auth)]
    )
    async def pause_job(job_id: str) -> JobInfo:
        try:
            return await backend.send_command(job_id, JobCommand(action="pause"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/api/jobs/{job_id}/resume", response_model=JobInfo, dependencies=[Depends(require_auth)]
    )
    async def resume_job(job_id: str) -> JobInfo:
        try:
            return await backend.send_command(job_id, JobCommand(action="resume"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/api/jobs/{job_id}/stop-seeding",
        response_model=JobInfo,
        dependencies=[Depends(require_auth)],
    )
    async def stop_seeding(job_id: str) -> JobInfo:
        try:
            return await backend.send_command(job_id, JobCommand(action="stop_seeding"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/api/jobs/{job_id}/cancel", response_model=JobInfo, dependencies=[Depends(require_auth)]
    )
    async def cancel_job(job_id: str, delete_files: bool = Query(default=False)) -> JobInfo:
        try:
            return await backend.send_command(
                job_id, JobCommand(action="cancel", delete_files=delete_files)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch(
        "/api/jobs/{job_id}/options", response_model=JobInfo, dependencies=[Depends(require_auth)]
    )
    async def update_job_options(job_id: str, patch: JobOptionsPatch) -> JobInfo:
        try:
            return await backend.send_command(
                job_id, JobCommand(action="update_options", options=patch)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
    async def delete_job(job_id: str, delete_files: bool = Query(default=False)) -> JSONResponse:
        deleted = await backend.delete_job(job_id, delete_files=delete_files)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        return JSONResponse({"deleted": True, "job_id": job_id})

    @app.get("/api/jobs/{job_id}/stream", dependencies=[Depends(require_auth)])
    async def stream_job_sse(
        request: Request,
        job_id: str,
        since_seq: int = Query(default=0, ge=0),
        max_duration: float = Query(default=120.0, ge=1.0, le=140.0),
    ) -> StreamingResponse:
        """SSE stream bounded below Modal's 150s request limit for clean auto-reconnect."""
        job = await backend.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

        async def _event_gen() -> AsyncIterator[bytes]:
            deadline = time.monotonic() + max_duration
            yield b"retry: 500\n\n"
            sub = aiter(backend.subscribe(job_id, since_seq=since_seq))
            pending: asyncio.Task[StreamEvent] | None = None
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or await request.is_disconnected():
                        break
                    if pending is None:
                        pending = asyncio.create_task(anext(sub))
                    wait_slice = min(remaining, 10.0)
                    done, _ = await asyncio.wait({pending}, timeout=wait_slice)
                    if not done:
                        if time.monotonic() >= deadline:
                            break
                        yield b": keepalive\n\n"
                        continue
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        break
                    finally:
                        pending = None
                    if event.type == "ping":
                        yield b": keepalive\n\n"
                    else:
                        payload = json.dumps(event.model_dump())
                        chunk = f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"
                        yield chunk.encode("utf-8")
            finally:
                if pending is not None and not pending.done():
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                        await pending
                with contextlib.suppress(Exception):
                    await sub.aclose()  # type: ignore[attr-defined]

        return StreamingResponse(
            _event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _serve_ws(ws: WebSocket, job_id: str | None, since_seq: int) -> None:
        token = _extract_token_from_ws(ws)
        try:
            _verify_token(token, resolved_token, resolved_anon)
        except HTTPException:
            await ws.close(code=4401, reason="Unauthorized")
            return

        await ws.accept()
        sub_task: asyncio.Task[None] | None = None

        async def _forward() -> None:
            async for event in backend.subscribe(job_id, since_seq=since_seq):
                await ws.send_text(json.dumps(event.model_dump()))

        sub_task = asyncio.create_task(_forward())
        try:
            while True:
                # Drain incoming client messages / pings until disconnect
                msg = await ws.receive_text()
                if msg.strip().lower() == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}))
        except WebSocketDisconnect:
            pass
        finally:
            if sub_task:
                sub_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sub_task

    @app.websocket("/ws/jobs")
    async def ws_all_jobs(ws: WebSocket) -> None:
        await _serve_ws(ws, None, 0)

    @app.websocket("/ws/jobs/{job_id}")
    async def ws_single_job(
        ws: WebSocket, job_id: str, since_seq: int = Query(default=0, ge=0)
    ) -> None:
        await _serve_ws(ws, job_id, since_seq)

    # --- Modal Volume File Explorer APIs ---

    @app.get("/api/files", response_model=list[FileEntryInfo], dependencies=[Depends(require_auth)])
    async def list_files(
        path: str = Query(default=""),
        recursive: bool = Query(default=False),
    ) -> list[FileEntryInfo]:
        try:
            clean = sanitize_volume_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await backend.volume.listdir(clean, recursive=recursive)

    @app.get("/api/files/stat", response_model=FileEntryInfo, dependencies=[Depends(require_auth)])
    async def stat_file(path: str = Query(...)) -> FileEntryInfo:
        try:
            clean = sanitize_volume_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        entry = await backend.volume.stat(clean)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")
        return entry

    @app.get("/api/files/download", dependencies=[Depends(require_auth)])
    async def download_file(
        request: Request,
        path: str = Query(...),
        inline: bool = Query(default=False),
    ) -> StreamingResponse:
        try:
            clean = sanitize_volume_path(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not clean:
            raise HTTPException(status_code=400, detail="File path is required.")

        entry = await backend.volume.stat(clean)
        if entry is None or entry.is_dir:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")

        disposition_kind = "inline" if inline else "attachment"
        encoded_name = quote(entry.name)
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": f"{disposition_kind}; filename*=UTF-8''{encoded_name}",
        }
        media_type = entry.mime_type or "application/octet-stream"

        range_header = request.headers.get("range")
        if range_header:
            try:
                start, end = _parse_http_range(range_header, entry.size)
            except ValueError as exc:
                raise HTTPException(
                    status_code=416,
                    detail=str(exc),
                    headers={"Content-Range": f"bytes */{entry.size}"},
                ) from exc
            length = end - start + 1
            headers["Content-Range"] = f"bytes {start}-{end}/{entry.size}"
            headers["Content-Length"] = str(length)
            return StreamingResponse(
                backend.volume.read_range(clean, start, end),
                status_code=206,
                media_type=media_type,
                headers=headers,
            )

        headers["Content-Length"] = str(entry.size)
        return StreamingResponse(
            backend.volume.read_range(clean, 0, None),
            status_code=200,
            media_type=media_type,
            headers=headers,
        )

    @app.get("/api/files/archive", dependencies=[Depends(require_auth)])
    async def download_archive(
        paths: list[str] = Query(...),
        name: str = Query(default="archive.zip"),
    ) -> StreamingResponse:
        try:
            cleaned = [sanitize_volume_path(p) for p in paths]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        entries = await collect_archive_files(backend.volume, cleaned)
        if not entries:
            raise HTTPException(
                status_code=404, detail="No files found for requested archive paths."
            )

        safe_name = PurePosixPath(name).name or "archive.zip"
        if not safe_name.lower().endswith(".zip"):
            safe_name += ".zip"
        encoded = quote(safe_name)
        return StreamingResponse(
            stream_zip_archive(backend.volume, entries),
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            },
        )

    @app.post("/api/files/rename", dependencies=[Depends(require_auth)])
    async def rename_file(req: RenameFileRequest) -> JSONResponse:
        try:
            await backend.volume.rename(req.src_path, req.dst_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse({"renamed": True, "src_path": req.src_path, "dst_path": req.dst_path})

    @app.delete("/api/files", dependencies=[Depends(require_auth)])
    async def remove_file(
        path: str = Query(...),
        recursive: bool = Query(default=True),
    ) -> JSONResponse:
        try:
            await backend.volume.remove(path, recursive=recursive)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except IsADirectoryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse({"deleted": True, "path": path})

    return app
