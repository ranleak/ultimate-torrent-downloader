"""Pure-Python multi-resolution Windows .ico generator (16, 32, 48, 256 px RGBA PNG-in-ICO)."""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)


def _encode_png_rgba(size: int, pixels: bytearray) -> bytes:
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # Filter type 0 (None)
        start = y * stride
        raw.extend(pixels[start : start + stride])
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _render_icon_rgba(size: int) -> bytearray:
    buf = bytearray(size * size * 4)
    radius = size * 0.22
    half = size / 2.0

    for y in range(size):
        for x in range(size):
            px = x + 0.5
            py = y + 0.5
            # Signed distance to rounded square
            dx = max(abs(px - half) - (half - radius - 1.0), 0.0)
            dy = max(abs(py - half) - (half - radius - 1.0), 0.0)
            dist = math.hypot(dx, dy) - radius
            alpha = max(0.0, min(1.0, 0.5 - dist))
            if alpha <= 0.0:
                continue

            # Diagonal gradient from blue (#3b82f6) to teal (#14b8a6)
            t = (px + py) / (2.0 * size)
            r = int(59 * (1.0 - t) + 20 * t)
            g = int(130 * (1.0 - t) + 184 * t)
            b = int(246 * (1.0 - t) + 166 * t)

            # White downward torrent arrow + base tray
            nx = px / size
            ny = py / size
            in_shaft = abs(nx - 0.5) <= 0.075 and 0.22 <= ny <= 0.58
            in_head = 0.50 <= ny <= 0.72 and abs(nx - 0.5) <= (0.72 - ny) * 1.05
            in_tray = 0.76 <= ny <= 0.83 and 0.24 <= nx <= 0.76
            if in_shaft or in_head or in_tray:
                r, g, b = 255, 255, 255

            idx = (y * size + x) * 4
            buf[idx] = r
            buf[idx + 1] = g
            buf[idx + 2] = b
            buf[idx + 3] = round(alpha * 255)
    return buf


def build_ico_bytes(sizes: tuple[int, ...] = (16, 32, 48, 256)) -> bytes:
    """Create a multi-resolution Windows `.ico` file containing RGBA PNG images."""
    images = [_encode_png_rgba(sz, _render_icon_rgba(sz)) for sz in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    dir_entries: list[bytes] = []
    offset = 6 + 16 * len(sizes)
    for sz, png_bytes in zip(sizes, images, strict=True):
        dim_byte = 0 if sz >= 256 else sz
        entry = struct.pack(
            "<BBBBHHII",
            dim_byte,
            dim_byte,
            0,
            0,
            1,
            32,
            len(png_bytes),
            offset,
        )
        dir_entries.append(entry)
        offset += len(png_bytes)
    return header + b"".join(dir_entries) + b"".join(images)


def write_ico(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_ico_bytes())
    return path


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("app.ico")
    write_ico(out)
