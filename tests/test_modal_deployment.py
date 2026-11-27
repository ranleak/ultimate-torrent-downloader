"""Regression tests for the source files mounted into Modal containers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("modal")

from modal._utils.async_utils import synchronizer
from modal.cli.import_refs import ImportRef, import_file_or_module
from modal.mount import _Mount

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_FILE = _REPO_ROOT / "src" / "utd" / "cloud" / "modal_app.py"


def test_file_deployment_mounts_importable_entrypoint(monkeypatch, tmp_path: Path) -> None:
    """Reproduce Modal's file-mode import and hydrate from only the mounted sources."""
    mounts = []
    for method_name in ("_from_local_file", "_from_local_python_packages"):
        original = getattr(_Mount, method_name)

        def capture(*args, _original=original, **kwargs):
            mount = _original(*args, **kwargs)
            mounts.append(mount)
            return mount

        monkeypatch.setattr(_Mount, method_name, capture)

    monkeypatch.delenv("UTD_DEPLOY", raising=False)
    monkeypatch.delenv("UTD_API_TOKEN", raising=False)
    # Avoid leaking Modal's sys.path/sys.modules changes into the other tests.
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.delitem(sys.modules, "modal_app", raising=False)
    module = import_file_or_module(ImportRef(str(_APP_FILE), use_module_mode=False))
    monkeypatch.delitem(sys.modules, "modal_app", raising=False)

    worker = synchronizer._translate_in(module.run_torrent_job)
    gateway = synchronizer._translate_in(module.WebGateway)._class_service_function
    assert worker._source_info.module_name == "modal_app"
    assert gateway._source_info.module_name == "modal_app"

    # Recreate the /root mount layout from Modal's own file-selection logic.
    for mount in mounts:
        for entry in mount.entries:
            for local_file, remote_file in entry.get_files_to_upload():
                relative_path = Path(str(remote_file)).relative_to("/root")
                target = tmp_path / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local_file, target)

    assert (tmp_path / "modal_app.py").is_file()
    assert (tmp_path / "utd" / "cloud" / "modal_app.py").is_file()
    assert not (tmp_path / "utd" / "client").exists()

    # -I ignores PYTHONPATH; explicitly remove any editable checkout from site-packages.
    code = """
import importlib, pathlib, sys
root, checkout = map(pathlib.Path, sys.argv[1:])
sys.path = [str(root)] + [
    p for p in sys.path if p and pathlib.Path(p).resolve() not in (checkout, checkout / 'src')
]
module = importlib.import_module('modal_app')
import utd
assert pathlib.Path(module.__file__).resolve() == root / 'modal_app.py'
assert pathlib.Path(utd.__file__).resolve() == root / 'utd' / '__init__.py'
assert module.run_torrent_job is not None
assert module.WebGateway is not None

# Modal mounts the entrypoint at the shallow path /root/modal_app.py. Execute
# with that exact __file__ too, without needing to write outside tmp_path.
import importlib.util
spec = importlib.util.spec_from_file_location('modal_app', '/root/modal_app.py')
container_module = importlib.util.module_from_spec(spec)
sys.modules['modal_app'] = container_module
exec(compile((root / 'modal_app.py').read_bytes(), spec.origin, 'exec'), container_module.__dict__)
assert container_module.WebGateway is not None
assert container_module.run_torrent_job is not None
"""
    env = os.environ.copy()
    env.pop("UTD_DEPLOY", None)
    env.pop("UTD_API_TOKEN", None)
    subprocess.run(
        [sys.executable, "-I", "-c", code, str(tmp_path), str(_REPO_ROOT)],
        cwd=tmp_path,
        env=env,
        check=True,
    )
