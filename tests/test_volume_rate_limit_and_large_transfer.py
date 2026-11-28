"""Regression tests for Modal VolumeListFiles rate limits and large folder accelerated transfers."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import os
import time
import zipfile
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import modal.exception
import pytest

from utd.client.bridge import LocalTransferManager, ModalBridgeClient, ModalBridgeError
from utd.client.config import ClientConfig
from utd.cloud.backend import LocalTorrentBackend
from utd.cloud.server import create_cloud_app
from utd.cloud.volume_store import ModalVolumeStore, VolumeStore


@dataclasses.dataclass(frozen=True)
class _FakeModalEntry:
    path: str
    type: int  # 1 = FILE, 2 = DIRECTORY
    mtime: int
    size: int


class _AioCallable:
    """Mimics Modal SDK methods exposing `.aio(...)`."""

    def __init__(self, fn) -> None:
        self._fn = fn

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._fn(*args, **kwargs)

    async def aio(self, *args: Any, **kwargs: Any) -> Any:
        return await self._fn(*args, **kwargs)


class _AioGenCallable:
    """Mimics Modal SDK async generator methods exposing `.aio(...)`."""

    def __init__(self, gen_fn) -> None:
        self._gen_fn = gen_fn

    async def aio(self, *args: Any, **kwargs: Any):
        async for item in self._gen_fn(*args, **kwargs):
            yield item


class FakeRateLimitedModalVolume:
    """In-memory + mounted directory fake of `modal.Volume` enforcing `VolumeListFiles` quotas."""

    def __init__(
        self,
        backing_dir: Path,
        *,
        mount_dir: Path | None = None,
        max_calls_per_window: int = 1000,
        window_seconds: float = 1.0,
        fail_first_n_listdir: int = 0,
    ) -> None:
        self.backing_dir = backing_dir.resolve()
        self.backing_dir.mkdir(parents=True, exist_ok=True)
        self.mount_dir = mount_dir.resolve() if mount_dir is not None else None
        if self.mount_dir is not None:
            self.mount_dir.mkdir(parents=True, exist_ok=True)

        self.max_calls_per_window = max_calls_per_window
        self.window_seconds = window_seconds
        self.fail_first_n_listdir = fail_first_n_listdir

        self.listdir_calls: list[tuple[str, bool]] = []
        self.read_file_calls: list[str] = []
        self.reload_calls: int = 0
        self.rate_limit_errors_raised: int = 0
        self._call_timestamps: deque[float] = deque()

        self.listdir = _AioCallable(self._listdir)
        self.read_file = _AioGenCallable(self._read_file)
        self.reload = _AioCallable(self._reload)
        self.remove_file = _AioCallable(self._remove_file)
        self.copy_files = _AioCallable(self._copy_files)

    def _check_listdir_rate_limit(self) -> None:
        now = time.monotonic()
        while self._call_timestamps and (now - self._call_timestamps[0]) > self.window_seconds:
            self._call_timestamps.popleft()

        if self.fail_first_n_listdir > 0:
            self.fail_first_n_listdir -= 1
            self.rate_limit_errors_raised += 1
            raise modal.exception.ResourceExhaustedError(
                "VolumeListFiles rate limit exceeded. Please wait and retry."
            )

        if len(self._call_timestamps) >= self.max_calls_per_window:
            self.rate_limit_errors_raised += 1
            raise modal.exception.ResourceExhaustedError(
                "VolumeListFiles rate limit exceeded. Please wait and retry."
            )

        self._call_timestamps.append(now)

    def _to_entry(self, item: Path) -> _FakeModalEntry:
        st = item.stat()
        rel = item.relative_to(self.backing_dir).as_posix()
        is_dir = item.is_dir()
        return _FakeModalEntry(
            path=rel,
            type=2 if is_dir else 1,
            mtime=int(st.st_mtime),
            size=0 if is_dir else st.st_size,
        )

    async def _listdir(self, sdk_path: str, *, recursive: bool = False) -> list[_FakeModalEntry]:
        self.listdir_calls.append((sdk_path, recursive))
        await asyncio.sleep(0.002)
        self._check_listdir_rate_limit()

        clean = sdk_path.strip("/")
        target = (self.backing_dir / clean).resolve() if clean else self.backing_dir
        if not target.exists():
            raise FileNotFoundError(f"No such file or directory: {sdk_path}")
        if target.is_file():
            return [self._to_entry(target)]

        out: list[_FakeModalEntry] = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(target):
                dp = Path(dirpath)
                for d in sorted(dirnames):
                    out.append(self._to_entry(dp / d))
                for f in sorted(filenames):
                    out.append(self._to_entry(dp / f))
        else:
            for child in sorted(target.iterdir()):
                out.append(self._to_entry(child))
        return out

    async def _read_file(self, sdk_path: str):
        self.read_file_calls.append(sdk_path)
        clean = sdk_path.strip("/")
        target = (self.backing_dir / clean).resolve()
        if not target.is_file():
            raise FileNotFoundError(sdk_path)
        data = target.read_bytes()
        step = 128 * 1024
        for idx in range(0, len(data), step):
            yield data[idx : idx + step]

    async def _reload(self) -> None:
        self.reload_calls += 1
        if self.mount_dir is None:
            return
        # Mirror backing_dir into mount_dir to simulate `volume.reload()`
        for dirpath, dirnames, filenames in os.walk(self.backing_dir):
            rel_dir = Path(dirpath).relative_to(self.backing_dir)
            dst_dir = self.mount_dir / rel_dir
            dst_dir.mkdir(parents=True, exist_ok=True)
            for d in dirnames:
                (dst_dir / d).mkdir(parents=True, exist_ok=True)
            for fname in filenames:
                src_f = Path(dirpath) / fname
                dst_f = dst_dir / fname
                dst_f.write_bytes(src_f.read_bytes())

    async def _remove_file(self, sdk_path: str, *, recursive: bool = False) -> None:
        clean = sdk_path.strip("/")
        target = (self.backing_dir / clean).resolve()
        if not target.exists():
            raise FileNotFoundError(sdk_path)
        if target.is_dir():
            if not recursive and any(target.iterdir()):
                raise IsADirectoryError(sdk_path)
            for child in sorted(target.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
                else:
                    child.rmdir()
            target.rmdir()
        else:
            target.unlink()

    async def _copy_files(
        self, src_paths: list[str], dst_path: str, *, recursive: bool = False
    ) -> None:
        _ = recursive
        dst_clean = dst_path.strip("/")
        dst = (self.backing_dir / dst_clean).resolve()
        for raw_src in src_paths:
            src = (self.backing_dir / raw_src.strip("/")).resolve()
            if not src.exists():
                raise FileNotFoundError(raw_src)
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
            else:
                for item in src.rglob("*"):
                    rel = item.relative_to(src)
                    out = dst / rel
                    if item.is_dir():
                        out.mkdir(parents=True, exist_ok=True)
                    else:
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(item.read_bytes())


class _ModalStoreCloudBackend(LocalTorrentBackend):
    """Cloud backend wired with `ModalVolumeStore` for end-to-end testing."""

    def __init__(self, root_dir: Path, modal_store: ModalVolumeStore) -> None:
        super().__init__(root_dir, use_aria2=False)
        self._modal_store = modal_store

    @property
    def volume(self) -> VolumeStore:
        return self._modal_store


async def _wait_all_completed(
    manager: LocalTransferManager, transfer_ids: list[str], max_wait_s: float = 15.0
) -> None:
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        items = {t.transfer_id: t for t in manager.list_transfers()}
        errors = [
            f"{tid}: {items[tid].error}" for tid in transfer_ids if items[tid].status == "error"
        ]
        if errors:
            pytest.fail(f"Transfers failed: {errors}")
        if all(items[tid].status == "completed" for tid in transfer_ids):
            return
        await asyncio.sleep(0.02)
    statuses = {tid: manager.list_transfers() for tid in transfer_ids}
    pytest.fail(f"Timed out waiting for transfers: {statuses}")


@pytest.mark.asyncio
async def test_large_folder_accelerated_transfer_avoids_volumelistfiles_rate_limit(
    tmp_path: Path,
) -> None:
    """Transferring a large nested folder must not flood Modal's VolumeListFiles RPC."""
    backing = tmp_path / "backing"
    mount = tmp_path / "mount"
    pack_dir = backing / "downloads" / "large-pack"

    expected_hashes: dict[str, str] = {}
    # 48 files across 6 subdirectories, including multi-chunk (>1 MiB), small, and 0-byte files.
    for sub_idx in range(6):
        sub_dir = pack_dir / f"disc_{sub_idx:02d}"
        sub_dir.mkdir(parents=True, exist_ok=True)
        for file_idx in range(8):
            if file_idx == 0 and sub_idx < 2:
                # >2.3 MiB -> 3 parallel 1 MiB Range chunks per file
                content = os.urandom(2 * 1024 * 1024 + 350_000)
            elif file_idx == 7:
                content = b""
            else:
                content = os.urandom(24_576 + file_idx * 1024)
            fname = f"track_{file_idx:02d}.bin"
            fpath = sub_dir / fname
            fpath.write_bytes(content)
            rel = fpath.relative_to(backing).as_posix()
            expected_hashes[rel] = hashlib.sha256(content).hexdigest()

    # Enforce a strict cap of at most 4 VolumeListFiles calls/sec.
    # Previously this 48-file transfer triggered >110 VolumeListFiles RPCs in <1s and crashed.
    fake_vol = FakeRateLimitedModalVolume(
        backing,
        mount_dir=mount,
        max_calls_per_window=4,
        window_seconds=1.0,
    )
    store = ModalVolumeStore(fake_vol, mount_root=mount, retry_base_delay=0.05)
    backend = _ModalStoreCloudBackend(backing, store)
    cloud_app = create_cloud_app(backend, api_token="tok")

    local_dl = tmp_path / "pc-dl"
    cfg = ClientConfig(
        modal_endpoint_url="http://cloud.test",
        api_token="tok",
        local_download_dir=str(local_dl),
        transfer_concurrency=4,
        transfer_chunk_mb=1,
    )
    bridge = ModalBridgeClient(
        lambda: cfg,
        transport=httpx.ASGITransport(app=cloud_app),
        rate_limit_base_delay=0.02,
    )
    manager = LocalTransferManager(bridge, lambda: cfg)

    try:
        items = await manager.start_transfers(["downloads/large-pack"])
        assert len(items) == 48
        await _wait_all_completed(manager, [i.transfer_id for i in items])

        # Only 2 VolumeListFiles calls needed for the entire 48-file multi-range transfer:
        # 1) stat("downloads/large-pack") via parent "/downloads"
        # 2) listdir("/downloads/large-pack", recursive=True)
        assert len(fake_vol.listdir_calls) == 2
        assert fake_vol.rate_limit_errors_raised == 0
        # Mounted volume reloaded once for multi-range seeks
        assert fake_vol.reload_calls == 1

        for rel_posix, expected_sha in expected_hashes.items():
            saved = local_dl / Path(*PurePosixPath(rel_posix).parts)
            assert saved.is_file(), f"Missing transferred file {saved}"
            assert hashlib.sha256(saved.read_bytes()).hexdigest() == expected_sha
    finally:
        await bridge.close()
        await backend.close()


@pytest.mark.asyncio
async def test_modal_volume_store_coalesces_and_retries_resource_exhausted(
    tmp_path: Path,
) -> None:
    """Concurrent listdir/stat calls coalesce and back off cleanly on ResourceExhaustedError."""
    backing = tmp_path / "backing"
    folder = backing / "downloads" / "burst"
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        (folder / f"part_{i:02d}.dat").write_bytes(b"x" * (i + 10))

    # Force the first 3 VolumeListFiles calls to raise modal.exception.ResourceExhaustedError
    fake_vol = FakeRateLimitedModalVolume(
        backing,
        fail_first_n_listdir=3,
    )
    store = ModalVolumeStore(
        fake_vol,
        max_retries=5,
        retry_base_delay=0.01,
        retry_max_delay=0.05,
    )

    # Fire 10 concurrent stat calls for sibling files in the same uncached folder
    results = await asyncio.gather(
        *(store.stat(f"downloads/burst/part_{i:02d}.dat") for i in range(10))
    )
    assert all(r is not None for r in results)
    assert [r.size for r in results if r is not None] == [i + 10 for i in range(10)]
    # All 10 concurrent sibling stats coalesced onto a single retried parent listdir
    # sequence (3 failures + 1 success = 4 calls).
    assert fake_vol.rate_limit_errors_raised == 3
    assert len(fake_vol.listdir_calls) == 4


@pytest.mark.asyncio
async def test_exhausted_rate_limit_returns_429_and_client_recovers(tmp_path: Path) -> None:
    """When VolumeListFiles stays rate-limited, Cloud returns 429 and client retries cleanly."""
    backing = tmp_path / "backing"
    target = backing / "downloads" / "recover" / "payload.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = b"resilient-transfer-payload" * 200
    target.write_bytes(data)

    # Server store has max_retries=1, so 3 consecutive failures exceed server retries once
    # (returning HTTP 429) and succeed on the client's automatic 429 retry.
    fake_vol = FakeRateLimitedModalVolume(
        backing,
        fail_first_n_listdir=3,
    )
    store = ModalVolumeStore(
        fake_vol,
        max_retries=1,
        retry_base_delay=0.01,
        retry_max_delay=0.02,
    )
    backend = _ModalStoreCloudBackend(backing, store)
    cloud_app = create_cloud_app(backend, api_token="tok")

    transport = httpx.ASGITransport(app=cloud_app)
    # Verify direct HTTP call sees 429 Too Many Requests with Retry-After when retries exhaust
    fake_vol_429 = FakeRateLimitedModalVolume(backing, fail_first_n_listdir=5)
    app_429 = create_cloud_app(
        _ModalStoreCloudBackend(
            backing,
            ModalVolumeStore(fake_vol_429, max_retries=1, retry_base_delay=0.005),
        ),
        api_token="tok",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_429), base_url="http://cloud.test"
    ) as direct_http:
        r429 = await direct_http.get(
            "/api/files",
            headers={"Authorization": "Bearer tok"},
            params={"path": "downloads/recover"},
        )
        assert r429.status_code == 429
        assert r429.headers.get("retry-after") == "2"
        assert "VolumeListFiles rate limit exceeded" in r429.json()["detail"]

    # Now verify ModalBridgeClient + LocalTransferManager transparently recover from 429
    local_dl = tmp_path / "pc-dl"
    cfg = ClientConfig(
        modal_endpoint_url="http://cloud.test",
        api_token="tok",
        local_download_dir=str(local_dl),
    )

    class _FastRetryHeaderTransport(httpx.AsyncBaseTransport):
        def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
            self._inner = inner

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            resp = await self._inner.handle_async_request(request)
            if resp.status_code == 429:
                resp.headers["Retry-After"] = "0.02"
            return resp

    bridge = ModalBridgeClient(
        lambda: cfg,
        transport=_FastRetryHeaderTransport(transport),
        rate_limit_retries=4,
        rate_limit_base_delay=0.01,
    )
    manager = LocalTransferManager(bridge, lambda: cfg)

    try:
        items = await manager.start_transfers(["downloads/recover"])
        assert len(items) == 1
        await _wait_all_completed(manager, [items[0].transfer_id])
        saved = local_dl / "downloads" / "recover" / "payload.bin"
        assert saved.read_bytes() == data
    finally:
        await bridge.close()
        await backend.close()


@pytest.mark.asyncio
async def test_batch_sibling_selection_and_cache_freshness_and_zip(tmp_path: Path) -> None:
    """Batch selection, size growth, rename/delete invalidation, and ZIP minimize listdir."""
    backing = tmp_path / "backing"
    folder = backing / "downloads" / "album"
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for i in range(25):
        p = folder / f"song_{i:02d}.mp3"
        p.write_bytes(f"track-{i}".encode() * 50)
        paths.append(f"downloads/album/song_{i:02d}.mp3")

    fake_vol = FakeRateLimitedModalVolume(backing)
    store = ModalVolumeStore(fake_vol, retry_base_delay=0.01)
    backend = _ModalStoreCloudBackend(backing, store)
    cloud_app = create_cloud_app(backend, api_token="tok")

    local_dl = tmp_path / "pc-dl"
    cfg = ClientConfig(
        modal_endpoint_url="http://cloud.test",
        api_token="tok",
        local_download_dir=str(local_dl),
        transfer_concurrency=3,
    )
    bridge = ModalBridgeClient(lambda: cfg, transport=httpx.ASGITransport(app=cloud_app))
    manager = LocalTransferManager(bridge, lambda: cfg)

    try:
        # 1. Selecting 25 sibling files + duplicate path collapses into 1 parent listdir
        #    and deduplicates overlapping file targets.
        items = await manager.start_transfers([*paths, paths[0]])
        assert len(items) == 25
        await _wait_all_completed(manager, [i.transfer_id for i in items])
        assert len(fake_vol.listdir_calls) == 1

        # 2. Simulate an in-progress file growing on Modal Volume after it was cached:
        #    explicit /api/files/stat (used for single-file start_transfers) sees the new size!
        growing = folder / "song_00.mp3"
        new_payload = b"grown-final-content-" * 400
        growing.write_bytes(new_payload)

        regrown_items = await manager.start_transfers(["downloads/album/song_00.mp3"])
        assert len(regrown_items) == 1
        assert regrown_items[0].total_bytes == len(new_payload)
        await _wait_all_completed(manager, [regrown_items[0].transfer_id])
        assert (local_dl / "downloads" / "album" / "song_00.mp3").read_bytes() == new_payload

        # 3. Streaming a ZIP of all 25 files uses collected entries with 0 extra stat listdirs
        calls_before_zip = len(fake_vol.listdir_calls)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cloud_app), base_url="http://cloud.test"
        ) as client:
            zresp = await client.get(
                "/api/files/archive",
                headers={"Authorization": "Bearer tok"},
                params={"paths": "downloads/album", "name": "album.zip"},
            )
            assert zresp.status_code == 200
            with zipfile.ZipFile(io.BytesIO(zresp.content)) as zf:
                assert len(zf.namelist()) == 25
                assert zf.read("album/song_00.mp3") == new_payload
        # Only 1 recursive listdir for collect_archive_files; 0 listdirs in stream_zip_archive
        assert len(fake_vol.listdir_calls) - calls_before_zip == 1

        # 4. Rename & delete invalidate cache immediately
        await bridge.request_json(
            "POST",
            "/api/files/rename",
            json_body={
                "src_path": "downloads/album/song_01.mp3",
                "dst_path": "downloads/album/song_01_renamed.mp3",
            },
        )
        with pytest.raises(ModalBridgeError) as exc1:
            await bridge.stream_file_request(
                "/api/files/download", params={"path": "downloads/album/song_01.mp3"}
            )
        assert exc1.value.status_code == 404
        await bridge.request_json(
            "DELETE",
            "/api/files",
            params={"path": "downloads/album/song_01_renamed.mp3", "recursive": "false"},
        )
        with pytest.raises(ModalBridgeError) as exc2:
            await bridge.stream_file_request(
                "/api/files/download", params={"path": "downloads/album/song_01_renamed.mp3"}
            )
        assert exc2.value.status_code == 404
    finally:
        await bridge.close()
        await backend.close()


@pytest.mark.asyncio
async def test_cold_cache_adaptive_subtree_prefetch_for_many_single_file_subdirs(
    tmp_path: Path,
) -> None:
    """On a cold container, single-file subdirs trigger 1 recursive ancestor prefetch."""
    backing = tmp_path / "backing"
    dataset_root = backing / "downloads" / "partitioned-dataset"
    for idx in range(30):
        part_dir = dataset_root / f"part-{idx:03d}"
        part_dir.mkdir(parents=True, exist_ok=True)
        (part_dir / "data.bin").write_bytes(f"partition-{idx}".encode())

    fake_vol = FakeRateLimitedModalVolume(backing)
    store = ModalVolumeStore(fake_vol, retry_base_delay=0.01)

    # Cold cache: stat 30 files across 30 distinct single-file subdirectories sequentially
    for idx in range(30):
        info = await store.stat(f"downloads/partitioned-dataset/part-{idx:03d}/data.bin")
        assert info is not None
        assert info.size == len(f"partition-{idx}".encode())

    # First 2 misses list their parent dirs (2 calls), 3rd miss triggers 1 recursive prefetch of
    # /downloads/partitioned-dataset, and the remaining 27 subdirectories hit RAM cache (0 calls)!
    assert len(fake_vol.listdir_calls) == 3


@pytest.mark.asyncio
async def test_transfer_manager_bounds_concurrent_active_files_and_clean_cancel(
    tmp_path: Path,
) -> None:
    """Large folder transfers bound concurrent open files and cancel queued items cleanly."""
    backing = tmp_path / "backing"
    folder = backing / "downloads" / "many-files"
    folder.mkdir(parents=True, exist_ok=True)
    for idx in range(20):
        (folder / f"item_{idx:02d}.bin").write_bytes(b"z" * 4096)

    fake_vol = FakeRateLimitedModalVolume(backing)
    store = ModalVolumeStore(fake_vol, retry_base_delay=0.01)
    backend = _ModalStoreCloudBackend(backing, store)
    cloud_app = create_cloud_app(backend, api_token="tok")

    peak_active_downloads = 0
    in_flight = 0

    class _SlowDownloadTransport(httpx.AsyncBaseTransport):
        def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
            self._inner = inner

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal peak_active_downloads, in_flight
            if request.url.path == "/api/files/download":
                in_flight += 1
                peak_active_downloads = max(peak_active_downloads, in_flight)
                try:
                    await asyncio.sleep(0.04)
                    return await self._inner.handle_async_request(request)
                finally:
                    in_flight -= 1
            return await self._inner.handle_async_request(request)

    local_dl = tmp_path / "pc-dl"
    cfg = ClientConfig(
        modal_endpoint_url="http://cloud.test",
        api_token="tok",
        local_download_dir=str(local_dl),
        transfer_concurrency=2,
    )
    bridge = ModalBridgeClient(
        lambda: cfg,
        transport=_SlowDownloadTransport(httpx.ASGITransport(app=cloud_app)),
    )
    manager = LocalTransferManager(bridge, lambda: cfg)

    try:
        items = await manager.start_transfers(["downloads/many-files"])
        assert len(items) == 20
        # Cancel the last 4 queued items immediately before they acquire a slot
        cancelled_ids = [t.transfer_id for t in items[-4:]]
        for tid in cancelled_ids:
            res = await manager.cancel_transfer(tid)
            assert res.status == "cancelled"

        remaining_ids = [t.transfer_id for t in items[:-4]]
        await _wait_all_completed(manager, remaining_ids)

        # With transfer_concurrency=2, active streams/files are bounded to max(4, 2*2) = 4
        assert peak_active_downloads <= 4
        # No leftover .utdpart files anywhere
        assert list(local_dl.rglob("*.utdpart")) == []
    finally:
        await bridge.close()
        await backend.close()
