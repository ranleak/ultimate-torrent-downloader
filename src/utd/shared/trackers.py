"""Curated high-reliability public BitTorrent trackers for faster peer discovery."""

from __future__ import annotations

PUBLIC_TRACKERS: tuple[str, ...] = (
    "udp://tracker.opentrackr.org:1337/announce",
    "https://tracker.opentrackr.org:443/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "https://tracker.tamersunion.org:443/announce",
    "https://tracker.gbitt.info:443/announce",
    "http://tracker.gbitt.info:80/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
)


def merged_trackers_csv(existing: list[str] | None = None, *, is_private: bool = False) -> str:
    """Return comma-separated trackers for aria2c `--bt-tracker`, respecting private torrents."""
    if is_private:
        return ""
    merged: list[str] = []
    seen: set[str] = set()
    for tr in [*(existing or []), *PUBLIC_TRACKERS]:
        cleaned = tr.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            merged.append(cleaned)
    return ",".join(merged)
