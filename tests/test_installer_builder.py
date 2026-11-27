"""Tests for the Linux -> Windows NSIS cross-compiling build script and icon generator."""

from __future__ import annotations

import struct
import sys
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "installer" / "windows"))

from generate_icon import build_ico_bytes  # noqa: E402

from build_windows_installer import (  # noqa: E402
    generate_nsis_manifests,
    patch_embed_pth,
    read_app_version,
    stage_windows_bundle,
)


def test_generate_multi_res_ico() -> None:
    ico = build_ico_bytes()
    reserved, img_type, count = struct.unpack("<HHH", ico[:6])
    assert reserved == 0
    assert img_type == 1
    assert count == 4

    # Verify each directory entry points to a valid PNG image
    for i in range(count):
        entry = ico[6 + i * 16 : 22 + i * 16]
        _w, _h, _colors, _res, _planes, bpp, size, offset = struct.unpack("<BBBBHHII", entry)
        assert bpp == 32
        assert ico[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert len(ico[offset : offset + size]) == size


def test_patch_embed_pth(tmp_path: Path) -> None:
    pth = tmp_path / "python312._pth"
    pth.write_text(
        "python312.zip\n.\n\n# Uncomment to run site.main()\n#import site\n", encoding="utf-8"
    )
    patched = patch_embed_pth(tmp_path)
    lines = patched.read_text(encoding="utf-8").splitlines()
    assert lines == ["python312.zip", ".", "site-packages", "app", "import site"]


def test_stage_windows_bundle_and_manifests(tmp_path: Path) -> None:
    embed_zip = tmp_path / "python-3.12.10-embed-amd64.zip"
    with zipfile.ZipFile(embed_zip, "w") as zf:
        zf.writestr("python.exe", b"MZ-stub")
        zf.writestr("pythonw.exe", b"MZ-stub-w")
        zf.writestr("python312.zip", b"PK")
        zf.writestr("python312._pth", "python312.zip\n.\n#import site\n")

    stage_dir = tmp_path / "stage"
    manifest_dir = tmp_path / "manifests"
    stage_windows_bundle(
        stage_dir=stage_dir,
        manifest_dir=manifest_dir,
        embed_zip=embed_zip,
        python_minor="3.12",
        skip_pip=True,
    )

    assert (stage_dir / "app.ico").is_file()
    assert (stage_dir / "python" / "pythonw.exe").is_file()
    assert (stage_dir / "python" / "app" / "utd" / "client" / "static" / "index.html").is_file()
    assert not (stage_dir / "python" / "app" / "utd" / "cloud" / "modal_app.py").exists()

    install_nsh = (manifest_dir / "files_install.nsh").read_text(encoding="utf-8")
    uninstall_nsh = (manifest_dir / "files_uninstall.nsh").read_text(encoding="utf-8")
    assert 'SetOutPath "$INSTDIR\\python\\app\\utd\\client\\static"' in install_nsh
    assert 'File "' in install_nsh
    assert 'Delete "$INSTDIR\\app.ico"' in uninstall_nsh
    assert 'RMDir "$INSTDIR\\python\\app\\utd\\client\\static"' in uninstall_nsh
    # Deepest dirs removed before parents
    idx_deep = uninstall_nsh.index('RMDir "$INSTDIR\\python\\app\\utd\\client\\static"')
    idx_parent = uninstall_nsh.index('RMDir "$INSTDIR\\python\\app\\utd"')
    assert idx_deep < idx_parent

    assert read_app_version() == "0.1.0"

    # Also verify regenerate manifests directly
    i2, u2 = generate_nsis_manifests(stage_dir, tmp_path / "m2")
    assert i2.is_file() and u2.is_file()
