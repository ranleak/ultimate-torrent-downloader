"""Version lookup helper without circular imports."""

from __future__ import annotations

from utd import __version__


def get_package_version() -> str:
    return __version__
