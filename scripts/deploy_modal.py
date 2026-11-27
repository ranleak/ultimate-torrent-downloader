"""Deploy Ultimate Torrent Downloader backend to Modal.com and configure local client."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from utd.client.config import load_config, save_config  # noqa: E402
from utd.cloud.config import APP_NAME, AUTH_SECRET_NAME  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="utd-deploy",
        description="Provision Modal auth secret, deploy cloud backend, and save client config.",
    )
    parser.add_argument("--token", default="", help="Use an explicit UTD API bearer token.")
    parser.add_argument(
        "--rotate-token",
        action="store_true",
        help="Generate a brand-new bearer token and overwrite the Modal secret.",
    )
    parser.add_argument(
        "--proxy-auth",
        action="store_true",
        help="Require Modal Proxy Auth tokens on the web gateway in addition to bearer auth.",
    )
    parser.add_argument("--environment", default="", help="Optional Modal environment name.")
    args = parser.parse_args(argv)

    import modal

    cfg = load_config()
    token = args.token.strip() or os.environ.get("UTD_API_TOKEN", "").strip()
    if args.rotate_token or not token:
        token = cfg.api_token.strip() if (not args.rotate_token and cfg.api_token.strip()) else ""
    if not token:
        token = secrets.token_urlsafe(32)

    env_name = args.environment or None
    if args.rotate_token:
        modal.Secret.objects.delete(AUTH_SECRET_NAME, allow_missing=True, environment_name=env_name)
    modal.Secret.objects.create(
        AUTH_SECRET_NAME,
        {"UTD_API_TOKEN": token},
        allow_existing=True,
        environment_name=env_name,
    )

    modal_app_path = _SRC_DIR / "utd" / "cloud" / "modal_app.py"
    cmd = ["modal", "deploy", str(modal_app_path)]
    if args.environment:
        cmd.extend(["--env", args.environment])

    env = os.environ.copy()
    env["UTD_DEPLOY"] = "1"
    if args.proxy_auth:
        env["UTD_REQUIRE_PROXY_AUTH"] = "1"

    print(f"Deploying Modal App '{APP_NAME}'...")
    subprocess.run(cmd, check=True, env=env, cwd=str(_REPO_ROOT))

    gateway_cls = modal.Cls.from_name(APP_NAME, "WebGateway", environment_name=env_name)
    endpoint_url = gateway_cls().web.get_web_url()
    if not endpoint_url:
        raise RuntimeError("Deployment succeeded, but failed to retrieve WebGateway URL.")

    cfg.modal_endpoint_url = endpoint_url.rstrip("/")
    cfg.api_token = token
    saved_path = save_config(cfg)

    print("\nDeployment complete!")
    print(f"  Modal Endpoint : {cfg.modal_endpoint_url}")
    print(f"  Auth Secret    : {AUTH_SECRET_NAME}")
    print(f"  Client Config  : {saved_path}")
    print("\nLaunch the local client with: uv run utd client")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
