"""Modal Volume and local filesystem explorer, Range reader, and streaming ZIP archive."""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
import shutil
import struct
import time
import zlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from utd.shared.models import FileEntryInfo, sanitize_volume_path

_READ_CHUNK_BYTES = 256 * 1024  # 256 KiB streaming chunks


class AsyncRWLock:
    """Async readers-writer lock preventing `volume.reload()` during active file reads."""

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False

    @contextlib.asynccontextmanager
    async def read_lock(self) -> AsyncIterator[None]:
        async with self._cond:
            while self._writer:
                await self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextlib.asynccontextmanager
    async def write_lock(self) -> AsyncIterator[None]:
        async with self._cond:
            while self._writer or self._readers > 0:
                await self._cond.wait()
            self._writer = True
        try:
            yield
        finally:
            async with self._cond:
                self._writer = False
                self._cond.notify_all()


def _guess_mime(name: str, is_dir: bool) -> str | None:
    if is_dir:
        return None
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


def _parse_range_bounds(size: int, start: int, end: int | None) -> tuple[int, int]:
    if start < 0:
        raise ValueError("Range start must be >= 0.")
    if size == 0:
        return 0, -1
    if start >= size:
        raise ValueError(f"Range start {start} exceeds file size {size}.")
    resolved_end = size - 1 if end is None else min(end, size - 1)
    if resolved_end < start:
        raise ValueError(f"Range end {resolved_end} is before start {start}.")
    return start, resolved_end


class VolumeStore(Protocol):
    """Unified file-explorer and streaming interface over Modal Volume or local storage."""

    async def listdir(self, path: str, *, recursive: bool = False) -> list[FileEntryInfo]: ...

    async def stat(self, path: str) -> FileEntryInfo | None: ...

    async def read_range(
        self, path: str, start: int = 0, end: int | None = None
    ) -> AsyncIterator[bytes]: ...

    async def remove(self, path: str, *, recursive: bool = False) -> None: ...

    async def rename(self, src_path: str, dst_path: str) -> None: ...


class LocalVolumeStore:
    """Local filesystem implementation of `VolumeStore` (used in tests and local mode)."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, rel_path: str) -> Path:
        clean = sanitize_volume_path(rel_path)
        target = (self.root / clean).resolve() if clean else self.root
        if target != self.root and self.root not in target.parents:
            raise ValueError("Resolved path escapes volume root.")
        return target

    def _to_entry(self, item: Path) -> FileEntryInfo:
        st = item.stat()
        rel = item.relative_to(self.root).as_posix()
        is_dir = item.is_dir()
        return FileEntryInfo(
            name=item.name,
            path=rel,
            is_dir=is_dir,
            size=0 if is_dir else st.st_size,
            mtime=int(st.st_mtime),
            mime_type=_guess_mime(item.name, is_dir),
        )

    async def listdir(self, path: str, *, recursive: bool = False) -> list[FileEntryInfo]:
        target = self._resolve(path)
        if not target.exists():
            return []
        if target.is_file():
            return [self._to_entry(target)]

        entries: list[FileEntryInfo] = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(target):
                dp = Path(dirpath)
                for d in sorted(dirnames):
                    entries.append(self._to_entry(dp / d))
                for f in sorted(filenames):
                    entries.append(self._to_entry(dp / f))
        else:
            for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                entries.append(self._to_entry(item))
        return entries

    async def stat(self, path: str) -> FileEntryInfo | None:
        clean = sanitize_volume_path(path)
        if not clean:
            return FileEntryInfo(name="", path="", is_dir=True, size=0, mtime=0)
        target = self._resolve(clean)
        if not target.exists():
            return None
        return self._to_entry(target)

    async def read_range(
        self, path: str, start: int = 0, end: int | None = None
    ) -> AsyncIterator[bytes]:
        target = self._resolve(path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        size = target.stat().st_size
        lo, hi = _parse_range_bounds(size, start, end)
        if hi < lo:
            return
        remaining = hi - lo + 1
        offset = lo

        def _read_chunk(pos: int, n: int) -> bytes:
            with target.open("rb") as fh:
                fh.seek(pos)
                return fh.read(n)

        while remaining > 0:
            to_read = min(_READ_CHUNK_BYTES, remaining)
            chunk = await asyncio.to_thread(_read_chunk, offset, to_read)
            if not chunk:
                break
            offset += len(chunk)
            remaining -= len(chunk)
            yield chunk

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        clean = sanitize_volume_path(path)
        if not clean:
            raise ValueError("Cannot delete the volume root.")
        target = self._resolve(clean)
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {path}")
        if target.is_dir():
            if not recursive and any(target.iterdir()):
                raise IsADirectoryError(f"Directory is not empty: {path}")
            await asyncio.to_thread(shutil.rmtree, target)
        else:
            await asyncio.to_thread(target.unlink)

    async def rename(self, src_path: str, dst_path: str) -> None:
        src_clean = sanitize_volume_path(src_path)
        dst_clean = sanitize_volume_path(dst_path)
        if not src_clean or not dst_clean:
            raise ValueError("Source and destination paths must be non-empty.")
        src = self._resolve(src_clean)
        dst = self._resolve(dst_clean)
        if not src.exists():
            raise FileNotFoundError(f"Source path not found: {src_path}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(src.rename, dst)


class ModalVolumeStore:
    """Modal Volume implementation using Modal SDK `.aio` APIs and RWLock-guarded Range reads."""

    def __init__(self, volume: Any, mount_root: Path | None = None) -> None:
        self.volume = volume
        self.mount_root = mount_root
        self._rwlock = AsyncRWLock()

    @staticmethod
    def _sdk_path(rel_path: str) -> str:
        clean = sanitize_volume_path(rel_path)
        return f"/{clean}" if clean else "/"

    @staticmethod
    def _convert_entry(entry: Any) -> FileEntryInfo:
        # Modal `FileEntry.type` is `FileEntryType` (FILE=1, DIRECTORY=2)
        type_val = getattr(entry.type, "value", entry.type)
        is_dir = int(type_val) == 2 or str(entry.type).endswith("DIRECTORY")
        rel = str(entry.path).lstrip("/")
        name = PurePosixPath(rel).name or rel
        size = 0 if is_dir else int(getattr(entry, "size", 0) or 0)
        mtime = int(getattr(entry, "mtime", 0) or 0)
        return FileEntryInfo(
            name=name,
            path=rel,
            is_dir=is_dir,
            size=size,
            mtime=mtime,
            mime_type=_guess_mime(name, is_dir),
        )

    async def listdir(self, path: str, *, recursive: bool = False) -> list[FileEntryInfo]:
        sdk_path = self._sdk_path(path)
        try:
            raw_entries = await self.volume.listdir.aio(sdk_path, recursive=recursive)
        except FileNotFoundError:
            return []
        entries = [self._convert_entry(e) for e in raw_entries]
        if not recursive:
            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    async def stat(self, path: str) -> FileEntryInfo | None:
        clean = sanitize_volume_path(path)
        if not clean:
            return FileEntryInfo(name="", path="", is_dir=True, size=0, mtime=0)
        sdk_path = self._sdk_path(clean)
        try:
            raw_entries = await self.volume.listdir.aio(sdk_path, recursive=False)
        except FileNotFoundError:
            return None
        for entry in raw_entries:
            converted = self._convert_entry(entry)
            if converted.path == clean:
                return converted
        # If listing a directory returned its children, confirm parent exists via parent listing.
        parent = str(PurePosixPath(clean).parent)
        parent_sdk = "/" if parent == "." else f"/{parent}"
        try:
            parent_entries = await self.volume.listdir.aio(parent_sdk, recursive=False)
        except FileNotFoundError:
            return None
        for entry in parent_entries:
            converted = self._convert_entry(entry)
            if converted.path == clean:
                return converted
        return None

    async def _ensure_mounted_fresh(self, clean_path: str, expected: FileEntryInfo) -> Path | None:
        if self.mount_root is None:
            return None
        local_file = (self.mount_root / clean_path).resolve()
        if self.mount_root not in local_file.parents:
            return None

        def _matches() -> bool:
            if not local_file.is_file():
                return False
            st = local_file.stat()
            return st.st_size == expected.size and int(st.st_mtime) >= expected.mtime

        if _matches():
            return local_file

        async with self._rwlock.write_lock():
            if not _matches():
                with contextlib.suppress(Exception):
                    await self.volume.reload.aio()
            if _matches():
                return local_file
        return None

    async def read_range(
        self, path: str, start: int = 0, end: int | None = None
    ) -> AsyncIterator[bytes]:
        clean = sanitize_volume_path(path)
        if not clean:
            raise FileNotFoundError("Cannot read directory root as a file.")
        info = await self.stat(clean)
        if info is None or info.is_dir:
            raise FileNotFoundError(f"File not found: {path}")

        lo, hi = _parse_range_bounds(info.size, start, end)
        if hi < lo:
            return

        # Whole-file stream uses Modal's parallel block reader directly (avoids busy locks).
        if lo == 0 and hi == info.size - 1:
            async for chunk in self.volume.read_file.aio(f"/{clean}"):
                if chunk:
                    yield chunk
            return

        # For HTTP Range seeks (e.g. parallel downloads / video scrubbing), use mounted O(1) seek
        # guarded by AsyncRWLock so reload() never collides with open file handles.
        local_file = await self._ensure_mounted_fresh(clean, info)
        if local_file is not None:
            remaining = hi - lo + 1
            offset = lo

            def _read_slice(pos: int, n: int) -> bytes:
                with local_file.open("rb") as fh:
                    fh.seek(pos)
                    return fh.read(n)

            while remaining > 0:
                to_read = min(_READ_CHUNK_BYTES, remaining)
                async with self._rwlock.read_lock():
                    chunk = await asyncio.to_thread(_read_slice, offset, to_read)
                if not chunk:
                    break
                offset += len(chunk)
                remaining -= len(chunk)
                yield chunk
            return

        # Safe fallback: slice from Modal SDK read_file stream.
        pos = 0
        async for chunk in self.volume.read_file.aio(f"/{clean}"):
            if not chunk:
                continue
            chunk_end = pos + len(chunk) - 1
            if chunk_end >= lo and pos <= hi:
                s_idx = max(0, lo - pos)
                e_idx = min(len(chunk), hi - pos + 1)
                yield chunk[s_idx:e_idx]
            pos += len(chunk)
            if pos > hi:
                break

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        clean = sanitize_volume_path(path)
        if not clean:
            raise ValueError("Cannot delete the volume root.")
        await self.volume.remove_file.aio(f"/{clean}", recursive=recursive)

    async def rename(self, src_path: str, dst_path: str) -> None:
        src_clean = sanitize_volume_path(src_path)
        dst_clean = sanitize_volume_path(dst_path)
        if not src_clean or not dst_clean:
            raise ValueError("Source and destination paths must be non-empty.")
        src_info = await self.stat(src_clean)
        if src_info is None:
            raise FileNotFoundError(f"Source path not found: {src_path}")
        await self.volume.copy_files.aio(
            [f"/{src_clean}"], f"/{dst_clean}", recursive=src_info.is_dir
        )
        await self.volume.remove_file.aio(f"/{src_clean}", recursive=src_info.is_dir)


def _dos_datetime(timestamp: int) -> tuple[int, int]:
    tm = time.localtime(timestamp or time.time())
    year = max(1980, min(2107, tm.tm_year))
    dos_time = (tm.tm_hour << 11) | (tm.tm_min << 5) | (tm.tm_sec // 2)
    dos_date = ((year - 1980) << 9) | (tm.tm_mon << 5) | tm.tm_mday
    return dos_time, dos_date


async def collect_archive_files(
    store: VolumeStore, paths: list[str]
) -> list[tuple[str, FileEntryInfo]]:
    """Expand requested file/directory paths into `(archive_rel_path, FileEntryInfo)` pairs."""
    collected: list[tuple[str, FileEntryInfo]] = []
    seen_paths: set[str] = set()

    for raw in paths:
        clean = sanitize_volume_path(raw)
        entry = await store.stat(clean)
        if entry is None:
            continue
        if not entry.is_dir:
            if entry.path not in seen_paths:
                seen_paths.add(entry.path)
                collected.append((entry.name, entry))
            continue

        base_name = PurePosixPath(clean).name if clean else "volume"
        prefix = f"{clean}/" if clean else ""
        children = await store.listdir(clean, recursive=True)
        for child in children:
            if child.is_dir or child.path in seen_paths:
                continue
            seen_paths.add(child.path)
            rel = (
                child.path[len(prefix) :]
                if prefix and child.path.startswith(prefix)
                else child.path
            )
            arc_name = f"{base_name}/{rel}" if base_name else rel
            collected.append((arc_name, child))
    return collected


async def stream_zip_archive(
    store: VolumeStore,
    entries: list[tuple[str, FileEntryInfo]],
    *,
    read_fn: Callable[[str], AsyncIterator[bytes]] | None = None,
) -> AsyncIterator[bytes]:
    """Stream a valid ZIP (with ZIP64 fallback when >4 GiB) on-the-fly without temp disk."""
    central_dir: list[bytes] = []
    offset = 0
    zip64_threshold = 0xFFFFFFFF

    for arc_name, entry in entries:
        name_bytes = arc_name.encode("utf-8")
        dos_time, dos_date = _dos_datetime(entry.mtime)
        use_zip64 = entry.size >= zip64_threshold or offset >= zip64_threshold
        ver_needed = 45 if use_zip64 else 20
        flags = 0x0800 | 0x0008  # UTF-8 + Data Descriptor

        local_header = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            ver_needed,
            flags,
            0,  # STORED
            dos_time,
            dos_date,
            0,
            0,
            0,
            len(name_bytes),
            0,
        )
        local_offset = offset
        head_chunk = local_header + name_bytes
        offset += len(head_chunk)
        yield head_chunk

        crc = 0
        actual_size = 0
        stream = read_fn(entry.path) if read_fn else store.read_range(entry.path, 0, None)
        async for chunk in stream:
            if not chunk:
                continue
            crc = zlib.crc32(chunk, crc) & 0xFFFFFFFF
            actual_size += len(chunk)
            offset += len(chunk)
            yield chunk

        # Data Descriptor
        if use_zip64 or actual_size >= zip64_threshold:
            descriptor = struct.pack("<IIQQ", 0x08074B50, crc, actual_size, actual_size)
            use_zip64 = True
        else:
            descriptor = struct.pack("<IIII", 0x08074B50, crc, actual_size, actual_size)
        offset += len(descriptor)
        yield descriptor

        # Central Directory File Header
        cd_extra = b""
        cd_comp = actual_size
        cd_uncomp = actual_size
        cd_offset = local_offset
        if actual_size >= zip64_threshold or local_offset >= zip64_threshold:
            z64_fields: list[int] = []
            if actual_size >= zip64_threshold:
                z64_fields.extend([actual_size, actual_size])
                cd_uncomp = zip64_threshold
                cd_comp = zip64_threshold
            if local_offset >= zip64_threshold:
                z64_fields.append(local_offset)
                cd_offset = zip64_threshold
            payload = struct.pack("<" + "Q" * len(z64_fields), *z64_fields)
            cd_extra = struct.pack("<HH", 0x0001, len(payload)) + payload
            ver_needed = 45

        cd_header = (
            struct.pack(
                "<IHHHHHHIIIHHHHHII",
                0x02014B50,
                (3 << 8) | ver_needed,
                ver_needed,
                flags,
                0,
                dos_time,
                dos_date,
                crc,
                cd_comp,
                cd_uncomp,
                len(name_bytes),
                len(cd_extra),
                0,
                0,
                0,
                0,
                cd_offset,
            )
            + name_bytes
            + cd_extra
        )
        central_dir.append(cd_header)

    cd_offset_start = offset
    cd_blob = b"".join(central_dir)
    cd_size = len(cd_blob)
    yield cd_blob

    num_entries = len(central_dir)
    needs_zip64_eocd = (
        num_entries >= 0xFFFF or cd_size >= zip64_threshold or cd_offset_start >= zip64_threshold
    )
    if needs_zip64_eocd:
        z64_eocd_offset = cd_offset_start + cd_size
        z64_eocd = struct.pack(
            "<IQHHIIQQQQ",
            0x06064B50,
            44,
            45,
            45,
            0,
            0,
            num_entries,
            num_entries,
            cd_size,
            cd_offset_start,
        )
        z64_locator = struct.pack("<IIQI", 0x07064B50, 0, z64_eocd_offset, 1)
        yield z64_eocd + z64_locator

    eocd = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        min(num_entries, 0xFFFF),
        min(num_entries, 0xFFFF),
        min(cd_size, zip64_threshold),
        min(cd_offset_start, zip64_threshold),
        0,
    )
    yield eocd
