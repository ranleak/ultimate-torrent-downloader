"""Unified CLI entrypoint for `utd` (`client`, `deploy`, `build-installer`)."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="utd",
        description="Ultimate Torrent Downloader CLI",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("client", help="Launch local HTTP server and HTML GUI", add_help=False)
    sub.add_parser("deploy", help="Deploy cloud backend to Modal.com", add_help=False)
    sub.add_parser("build-installer", help="Build Windows .exe installer on Linux", add_help=False)

    ns, rest = parser.parse_known_args(argv)
    cmd = ns.command or "client"

    if cmd == "client":
        from utd.client.launcher import run_client

        return run_client(rest)
    if cmd == "deploy":
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root))
        from scripts.deploy_modal import main as deploy_main

        return deploy_main(rest)
    if cmd == "build-installer":
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root))
        from scripts.build_windows_installer import main as build_main

        return build_main(rest)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
