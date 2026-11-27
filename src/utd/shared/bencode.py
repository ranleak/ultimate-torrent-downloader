"""Zero-dependency Bencode decoder/encoder and .torrent metadata inspector."""

from __future__ import annotations

import hashlib
from typing import Any

from utd.shared.models import TorrentFileSpec, TorrentMetadata


class BencodeError(ValueError):
    """Raised when bencoded torrent data is malformed."""


def _decode_at(data: bytes, idx: int) -> tuple[Any, int]:
    if idx >= len(data):
        raise BencodeError("Unexpected end of bencode stream.")
    token = data[idx : idx + 1]
    if token == b"i":
        end = data.find(b"e", idx + 1)
        if end == -1:
            raise BencodeError("Unterminated bencode integer.")
        raw_num = data[idx + 1 : end]
        if (
            not raw_num
            or (raw_num.startswith(b"-0"))
            or (len(raw_num) > 1 and raw_num[0:1] == b"0")
        ):
            raise BencodeError(f"Invalid bencode integer: {raw_num!r}")
        try:
            return int(raw_num), end + 1
        except ValueError as exc:
            raise BencodeError(f"Invalid bencode integer: {raw_num!r}") from exc

    if token == b"l":
        items: list[Any] = []
        pos = idx + 1
        while pos < len(data) and data[pos : pos + 1] != b"e":
            val, pos = _decode_at(data, pos)
            items.append(val)
        if pos >= len(data):
            raise BencodeError("Unterminated bencode list.")
        return items, pos + 1

    if token == b"d":
        mapping: dict[bytes, Any] = {}
        pos = idx + 1
        while pos < len(data) and data[pos : pos + 1] != b"e":
            key, pos = _decode_at(data, pos)
            if not isinstance(key, bytes):
                raise BencodeError("Bencode dictionary keys must be byte strings.")
            val, pos = _decode_at(data, pos)
            mapping[key] = val
        if pos >= len(data):
            raise BencodeError("Unterminated bencode dictionary.")
        return mapping, pos + 1

    colon = data.find(b":", idx)
    if colon == -1:
        raise BencodeError(f"Invalid bencode token at offset {idx}.")
    raw_len = data[idx:colon]
    if not raw_len.isdigit():
        raise BencodeError(f"Invalid bencode byte string length: {raw_len!r}")
    length = int(raw_len)
    start = colon + 1
    end = start + length
    if end > len(data):
        raise BencodeError("Truncated bencode byte string.")
    return data[start:end], end


def bdecode(data: bytes) -> Any:
    """Decode a bencoded byte buffer."""
    val, next_idx = _decode_at(data, 0)
    if next_idx != len(data):
        raise BencodeError("Trailing bytes after bencode root object.")
    return val


def bencode(value: Any) -> bytes:
    """Encode Python primitives into canonical bencode bytes."""
    if isinstance(value, bool):
        return b"i1e" if value else b"i0e"
    if isinstance(value, int):
        return f"i{value}e".encode("ascii")
    if isinstance(value, bytes):
        return f"{len(value)}:".encode("ascii") + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return f"{len(raw)}:".encode("ascii") + raw
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items: list[tuple[bytes, Any]] = []
        for k, v in value.items():
            key_bytes = k if isinstance(k, bytes) else str(k).encode("utf-8")
            items.append((key_bytes, v))
        items.sort(key=lambda pair: pair[0])
        out = [b"d"]
        for k_bytes, v in items:
            out.append(f"{len(k_bytes)}:".encode("ascii") + k_bytes)
            out.append(bencode(v))
        out.append(b"e")
        return b"".join(out)
    raise BencodeError(f"Unsupported bencode type: {type(value).__name__}")


def _decode_text(raw: bytes | None, fallback: str = "") -> str:
    if raw is None:
        return fallback
    return raw.decode("utf-8", errors="replace")


def inspect_torrent_bytes(data: bytes) -> TorrentMetadata:
    """Parse a `.torrent` file and return structured metadata."""
    root = bdecode(data)
    if not isinstance(root, dict) or b"info" not in root:
        raise BencodeError("Invalid torrent file: missing top-level 'info' dictionary.")
    info = root[b"info"]
    if not isinstance(info, dict):
        raise BencodeError("Invalid torrent file: 'info' must be a dictionary.")

    info_hash = hashlib.sha1(bencode(info)).hexdigest()
    name = _decode_text(info.get(b"name.utf-8") or info.get(b"name"), fallback=info_hash[:12])
    is_private = bool(info.get(b"private", 0))

    files: list[TorrentFileSpec] = []
    if b"files" in info and isinstance(info[b"files"], list):
        total_size = 0
        for idx, entry in enumerate(info[b"files"], start=1):
            if not isinstance(entry, dict):
                continue
            length = int(entry.get(b"length", 0))
            raw_parts = entry.get(b"path.utf-8") or entry.get(b"path") or []
            parts = [_decode_text(p) for p in raw_parts if isinstance(p, bytes)]
            rel_path = "/".join([name, *parts]) if parts else f"{name}/file_{idx}"
            files.append(TorrentFileSpec(index=idx, path=rel_path, size=length))
            total_size += length
    else:
        total_size = int(info.get(b"length", 0))
        files.append(TorrentFileSpec(index=1, path=name, size=total_size))

    trackers: list[str] = []
    seen: set[str] = set()
    announce = root.get(b"announce")
    if isinstance(announce, bytes):
        tr = _decode_text(announce).strip()
        if tr and tr not in seen:
            seen.add(tr)
            trackers.append(tr)
    announce_list = root.get(b"announce-list")
    if isinstance(announce_list, list):
        for tier in announce_list:
            if not isinstance(tier, list):
                continue
            for item in tier:
                if isinstance(item, bytes):
                    tr = _decode_text(item).strip()
                    if tr and tr not in seen:
                        seen.add(tr)
                        trackers.append(tr)

    comment = (
        _decode_text(root.get(b"comment")) if isinstance(root.get(b"comment"), bytes) else None
    )
    return TorrentMetadata(
        name=name,
        info_hash=info_hash,
        total_size=total_size,
        files=files,
        trackers=trackers,
        is_private=is_private,
        comment=comment,
    )
