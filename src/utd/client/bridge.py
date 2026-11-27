"""Async bridge to the Modal FastAPI server and parallel multi-range local transfer manager."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets
from pydantic import BaseModel, Field

from utd.client.config import ClientConfig

_WIN_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')


def sanitize_local_relpath(remote_path: str) -> Path:
    """Convert a Modal Volume POSIX path into a safe local relative filesystem path."""
    cleaned = remote_path.replace("\\", "/").strip().lstrip("/")
    parts: list[str] = []
    for part in PurePosixPath(cleaned).parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            raise ValueError("Path traversal ('..') is not allowed.")
        safe_part = _WIN_UNSAFE.sub("_", part).rstrip(". ")
        parts.append(safe_part or "_")
    if not parts:
        raise ValueError("Remote path cannot be empty.")
    return Path(*parts)


class TransferItem(BaseModel):
    transfer_id: str
    remote_path: str
    local_path: str
    filename: str
    total_bytes: int = 0
    transferred_bytes: int = 0
    speed_bps: int = 0
    progress: float = 0.0
    status: str = "queued"  # queued | transferring | completed | cancelled | error
    error: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class ModalBridgeError(RuntimeError):
    """Raised when the remote Modal API rejects or fails a request."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ModalBridgeClient:
    """Pooled async HTTP + WebSocket/SSE client talking to the deployed Modal backend."""

    def __init__(
        self,
        config_getter: Callable[[], ClientConfig],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._transport = transport
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0, read=140.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            follow_redirects=True,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _base_url(self) -> str:
        url = self._config_getter().normalized_endpoint()
        if not url:
            raise ModalBridgeError(
                503, "Modal endpoint URL is not configured. Open Settings to connect."
            )
        return url

    def _headers(self) -> dict[str, str]:
        token = self._config_getter().api_token.strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def ws_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        base = self._base_url()
        if base.startswith("https://"):
            ws_base = "wss://" + base[8:]
        elif base.startswith("http://"):
            ws_base = "ws://" + base[7:]
        else:
            ws_base = base
        query_items = dict(params or {})
        token = self._config_getter().api_token.strip()
        if token:
            query_items["token"] = token
        qs = f"?{urlencode(query_items)}" if query_items else ""
        return f"{ws_base}{path}{qs}"

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Any = None,
        data: Any = None,
    ) -> Any:
        url = f"{self._base_url()}{path}"
        try:
            resp = await self._client.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
                files=files,
                data=data,
            )
        except httpx.HTTPError as exc:
            raise ModalBridgeError(502, f"Upstream Modal request failed: {exc}") from exc

        if resp.status_code >= 400:
            try:
                err_json = resp.json()
                detail = str(err_json.get("detail", resp.text))
            except Exception:
                detail = resp.text or f"HTTP {resp.status_code}"
            raise ModalBridgeError(resp.status_code, detail)
        return resp.json()

    async def test_connection(self, endpoint_url: str, api_token: str) -> dict[str, Any]:
        base = endpoint_url.strip().rstrip("/")
        if not base:
            raise ModalBridgeError(400, "Endpoint URL cannot be empty.")
        headers = {"Authorization": f"Bearer {api_token.strip()}"} if api_token.strip() else {}
        t0 = time.monotonic()
        try:
            h_resp = await self._client.get(f"{base}/health", headers=headers)
            h_resp.raise_for_status()
            health_data = h_resp.json()
            # Verify authenticated endpoint access too
            f_resp = await self._client.get(
                f"{base}/api/files", headers=headers, params={"path": ""}
            )
            if f_resp.status_code >= 400:
                detail = f_resp.text
                with contextlib.suppress(Exception):
                    detail = str(f_resp.json().get("detail", detail))
                raise ModalBridgeError(f_resp.status_code, detail)
        except ModalBridgeError:
            raise
        except httpx.HTTPError as exc:
            raise ModalBridgeError(502, f"Connection failed: {exc}") from exc
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency_ms, "health": health_data}

    async def stream_file_request(
        self,
        path: str,
        *,
        params: list[tuple[str, str]] | dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        url = f"{self._base_url()}{path}"
        headers = {**self._headers(), **(extra_headers or {})}
        req = self._client.build_request("GET", url, headers=headers, params=params)
        resp = await self._client.send(req, stream=True)
        if resp.status_code >= 400:
            body = await resp.aread()
            await resp.aclose()
            detail = body.decode("utf-8", errors="replace")
            with contextlib.suppress(Exception):
                detail = str(json.loads(detail).get("detail", detail))
            raise ModalBridgeError(resp.status_code, detail)

        resp_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower()
            in {
                "content-type",
                "content-length",
                "content-range",
                "accept-ranges",
                "content-disposition",
            }
        }

        async def _iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                    if chunk:
                        yield chunk
            finally:
                await resp.aclose()

        return resp.status_code, resp_headers, _iter()

    async def stream_sse_lines(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> AsyncIterator[bytes]:
        url = f"{self._base_url()}{path}"
        async with self._client.stream(
            "GET",
            url,
            headers={**self._headers(), "Accept": "text/event-stream"},
            params=params,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ModalBridgeError(resp.status_code, body.decode("utf-8", errors="replace"))
            async for chunk in resp.aiter_bytes():
                yield chunk

    async def open_upstream_ws(self, path: str, params: dict[str, Any] | None = None):
        uri = self.ws_url(path, params=params)
        return await websockets.connect(
            uri, ping_interval=20, ping_timeout=20, max_size=2 * 1024 * 1024
        )


def _prepare_download_base(local_download_dir: str) -> Path:
    base_dir = Path(local_download_dir).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


class LocalTransferManager:
    """Downloads Modal Volume files/folders to local PC disk via parallel HTTP Range streams."""

    def __init__(
        self,
        bridge: ModalBridgeClient,
        config_getter: Callable[[], ClientConfig],
    ) -> None:
        self._bridge = bridge
        self._config_getter = config_getter
        self._transfers: dict[str, TransferItem] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    async def _emit(self, item: TransferItem) -> None:
        payload = {"type": "transfer", "data": item.model_dump()}
        for q in list(self._subscribers):
            await q.put(payload)

    def list_transfers(self) -> list[TransferItem]:
        return sorted(self._transfers.values(), key=lambda t: t.created_at, reverse=True)

    async def start_transfers(self, remote_paths: list[str]) -> list[TransferItem]:
        cfg = self._config_getter()
        base_dir = await asyncio.to_thread(_prepare_download_base, cfg.local_download_dir)

        file_jobs: list[tuple[str, Path, int]] = []
        for raw_path in remote_paths:
            stat = await self._bridge.request_json(
                "GET", "/api/files/stat", params={"path": raw_path}
            )
            if not stat["is_dir"]:
                rel = sanitize_local_relpath(stat["path"])
                file_jobs.append((stat["path"], base_dir / rel, int(stat["size"])))
            else:
                children = await self._bridge.request_json(
                    "GET", "/api/files", params={"path": stat["path"], "recursive": "true"}
                )
                for child in children:
                    if not child["is_dir"]:
                        rel = sanitize_local_relpath(child["path"])
                        file_jobs.append((child["path"], base_dir / rel, int(child["size"])))

        created: list[TransferItem] = []
        for r_path, l_path, size in file_jobs:
            tid = uuid.uuid4().hex[:12]
            item = TransferItem(
                transfer_id=tid,
                remote_path=r_path,
                local_path=str(l_path),
                filename=l_path.name,
                total_bytes=size,
                status="queued",
            )
            self._transfers[tid] = item
            cancel_evt = asyncio.Event()
            self._cancel_flags[tid] = cancel_evt
            self._tasks[tid] = asyncio.create_task(self._run_transfer(item, l_path, cancel_evt))
            await self._emit(item)
            created.append(item)
        return created

    async def cancel_transfer(self, transfer_id: str) -> TransferItem:
        item = self._transfers.get(transfer_id)
        if item is None:
            raise KeyError(f"Transfer '{transfer_id}' not found.")
        evt = self._cancel_flags.get(transfer_id)
        if evt:
            evt.set()
        task = self._tasks.get(transfer_id)
        if task and not task.done():
            task.cancel()
        item.status = "cancelled"
        item.speed_bps = 0
        item.updated_at = time.time()
        await self._emit(item)
        return item

    def remove_transfer(self, transfer_id: str) -> bool:
        if transfer_id not in self._transfers:
            return False
        self._transfers.pop(transfer_id, None)
        self._cancel_flags.pop(transfer_id, None)
        self._tasks.pop(transfer_id, None)
        return True

    async def _run_transfer(
        self,
        item: TransferItem,
        local_path: Path,
        cancel_evt: asyncio.Event,
    ) -> None:
        cfg = self._config_getter()
        concurrency = max(1, cfg.transfer_concurrency)
        chunk_size = max(1, cfg.transfer_chunk_mb) * 1024 * 1024
        part_path = local_path.with_name(f"{local_path.name}.utdpart")
        local_path.parent.mkdir(parents=True, exist_ok=True)

        item.status = "transferring"
        item.updated_at = time.time()
        await self._emit(item)

        total = item.total_bytes
        write_lock = threading.Lock()
        bytes_lock = asyncio.Lock()
        started = time.monotonic()
        last_emit = started

        try:
            if total == 0:
                part_path.write_bytes(b"")
                part_path.replace(local_path)
                item.status = "completed"
                item.progress = 100.0
                item.updated_at = time.time()
                await self._emit(item)
                return

            with part_path.open("wb") as fh:
                fh.truncate(total)

            # Build range chunks
            ranges: list[tuple[int, int]] = []
            offset = 0
            while offset < total:
                end = min(total - 1, offset + chunk_size - 1)
                ranges.append((offset, end))
                offset = end + 1

            range_q: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
            for r in ranges:
                range_q.put_nowait(r)

            fh_handle = part_path.open("r+b")
            try:

                def _write_at(pos: int, data: bytes) -> None:
                    with write_lock:
                        fh_handle.seek(pos)
                        fh_handle.write(data)

                async def _worker() -> None:
                    nonlocal last_emit
                    while not cancel_evt.is_set():
                        try:
                            start_b, end_b = range_q.get_nowait()
                        except asyncio.QueueEmpty:
                            return

                        attempt = 0
                        while True:
                            attempt += 1
                            chunk_written = 0
                            expected_len = end_b - start_b + 1
                            try:
                                status_code, _, body_iter = await self._bridge.stream_file_request(
                                    "/api/files/download",
                                    params={"path": item.remote_path},
                                    extra_headers={"Range": f"bytes={start_b}-{end_b}"},
                                )
                                async with contextlib.aclosing(body_iter):
                                    if status_code != 206 and (start_b > 0 or len(ranges) > 1):
                                        raise RuntimeError(
                                            f"Upstream returned HTTP {status_code} instead of 206 "
                                            f"Partial Content for range {start_b}-{end_b}."
                                        )
                                    cursor = start_b
                                    async for chunk in body_iter:
                                        if cancel_evt.is_set():
                                            return
                                        remaining = expected_len - chunk_written
                                        if remaining <= 0:
                                            break
                                        piece = chunk[:remaining]
                                        await asyncio.to_thread(_write_at, cursor, piece)
                                        cursor += len(piece)
                                        chunk_written += len(piece)
                                        async with bytes_lock:
                                            item.transferred_bytes += len(piece)
                                            now = time.monotonic()
                                            elapsed = max(0.05, now - started)
                                            item.speed_bps = int(item.transferred_bytes / elapsed)
                                            item.progress = round(
                                                min(
                                                    100.0, (item.transferred_bytes / total) * 100.0
                                                ),
                                                2,
                                            )
                                            if now - last_emit >= 0.25:
                                                last_emit = now
                                                item.updated_at = time.time()
                                                await self._emit(item)
                                if chunk_written != expected_len and not cancel_evt.is_set():
                                    raise RuntimeError(
                                        f"Truncated range {start_b}-{end_b}: "
                                        f"got {chunk_written}/{expected_len} bytes."
                                    )
                                break
                            except Exception:
                                if chunk_written:
                                    async with bytes_lock:
                                        item.transferred_bytes = max(
                                            0, item.transferred_bytes - chunk_written
                                        )
                                if attempt >= 3 or cancel_evt.is_set():
                                    raise
                                await asyncio.sleep(0.4 * attempt)

                workers = [
                    asyncio.create_task(_worker()) for _ in range(min(concurrency, len(ranges)))
                ]
                await asyncio.gather(*workers)
            finally:
                fh_handle.close()

            if cancel_evt.is_set():
                part_path.unlink(missing_ok=True)
                return

            part_path.replace(local_path)
            elapsed = max(0.01, time.monotonic() - started)
            item.transferred_bytes = total
            item.progress = 100.0
            item.speed_bps = int(total / elapsed)
            item.status = "completed"
            item.updated_at = time.time()
            await self._emit(item)

        except asyncio.CancelledError:
            part_path.unlink(missing_ok=True)
            item.status = "cancelled"
            item.speed_bps = 0
            item.updated_at = time.time()
            await self._emit(item)
        except Exception as exc:
            part_path.unlink(missing_ok=True)
            item.status = "error"
            item.error = str(exc)
            item.speed_bps = 0
            item.updated_at = time.time()
            await self._emit(item)
