"""Entrypoint for `python -m utd.client`."""

from __future__ import annotations

import sys

from utd.client.launcher import run_client

if __name__ == "__main__":
    raise SystemExit(run_client(sys.argv[1:]))
