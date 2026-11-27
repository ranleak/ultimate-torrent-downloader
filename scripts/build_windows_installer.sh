#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if command -v uv >/dev/null 2>&1; then
  exec uv run python "${REPO_ROOT}/scripts/build_windows_installer.py" "$@"
else
  exec python3 "${REPO_ROOT}/scripts/build_windows_installer.py" "$@"
fi
