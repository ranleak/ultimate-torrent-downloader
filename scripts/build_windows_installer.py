"""Cross-compile the UTD Windows .exe installer natively on Linux using NSIS (makensis)."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INSTALLER_DIR = _REPO_ROOT / "installer" / "windows"
if str(_INSTALLER_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALLER_DIR))

from generate_icon import write_ico  # noqa: E402

DEFAULT_PYTHON_VERSION = "3.12.10"
DEFAULT_EMBED_URL_TEMPLATE = (
    "https://www.python.org/ftp/python/{version}/python-{version}-embed-amd64.zip"
)
CLIENT_RUNTIME_DEPS = (
    "fastapi>=0.115.0",
    "httpx>=0.28.0",
    "pydantic>=2.10.0",
    "python-multipart>=0.0.20",
    "uvicorn>=0.34.0",
    "websockets>=14.0",
)


def read_app_version(pyproject_path: Path | None = None) -> str:
    target = pyproject_path or (_REPO_ROOT / "pyproject.toml")
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            _, val = stripped.split("=", 1)
            return val.strip().strip('"').strip("'")
    return "0.1.0"


def patch_embed_pth(python_dir: Path) -> Path:
    """Update `python3XX._pth` inside embeddable Python so `site-packages` and `app` resolve."""
    pth_files = sorted(python_dir.glob("python*._pth"))
    if not pth_files:
        raise FileNotFoundError(f"No python*._pth file found in {python_dir}")
    pth_file = pth_files[0]

    lines = [
        line.strip()
        for line in pth_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    zip_entries = [line for line in lines if line.endswith(".zip")]
    ordered: list[str] = [*zip_entries, ".", "site-packages", "app", "import site"]
    deduped: list[str] = []
    for item in ordered:
        if item not in deduped:
            deduped.append(item)
    pth_file.write_text("\n".join(deduped) + "\n", encoding="utf-8")
    return pth_file


def generate_nsis_manifests(stage_dir: Path, manifest_dir: Path) -> tuple[Path, Path]:
    """Generate deterministic `files_install.nsh` and `files_uninstall.nsh` from `stage_dir`."""
    manifest_dir.mkdir(parents=True, exist_ok=True)
    files_by_dir: dict[str, list[Path]] = {}
    all_dirs: set[str] = set()

    for path in sorted(stage_dir.rglob("*")):
        rel = path.relative_to(stage_dir)
        if path.is_dir():
            all_dirs.add(rel.as_posix())
            continue
        parent_rel = rel.parent.as_posix()
        key = "" if parent_rel == "." else parent_rel
        if key:
            all_dirs.add(key)
        files_by_dir.setdefault(key, []).append(path)

    install_lines: list[str] = []
    for rel_dir in sorted(files_by_dir):
        win_sub = rel_dir.replace("/", "\\")
        out_dir = f"$INSTDIR\\{win_sub}" if win_sub else "$INSTDIR"
        install_lines.append(f'SetOutPath "{out_dir}"')
        for file_path in files_by_dir[rel_dir]:
            install_lines.append(f'File "{file_path}"')

    uninstall_lines: list[str] = []
    for rel_dir in sorted(files_by_dir, reverse=True):
        win_sub = rel_dir.replace("/", "\\")
        prefix = f"$INSTDIR\\{win_sub}" if win_sub else "$INSTDIR"
        for file_path in reversed(files_by_dir[rel_dir]):
            uninstall_lines.append(f'Delete "{prefix}\\{file_path.name}"')

    for rel_dir in sorted(all_dirs, key=lambda d: (d.count("/"), d), reverse=True):
        win_sub = rel_dir.replace("/", "\\")
        uninstall_lines.append(f'RMDir "$INSTDIR\\{win_sub}"')

    install_nsh = manifest_dir / "files_install.nsh"
    uninstall_nsh = manifest_dir / "files_uninstall.nsh"
    install_nsh.write_text("\n".join(install_lines) + "\n", encoding="utf-8")
    uninstall_nsh.write_text("\n".join(uninstall_lines) + "\n", encoding="utf-8")
    return install_nsh, uninstall_nsh


def _download_embeddable_python(
    *,
    python_version: str,
    python_url: str | None,
    python_zip: Path | None,
    cache_dir: Path,
) -> Path:
    if python_zip is not None:
        if not python_zip.is_file():
            raise FileNotFoundError(f"Embeddable Python archive not found: {python_zip}")
        return python_zip

    cache_dir.mkdir(parents=True, exist_ok=True)
    url = python_url or DEFAULT_EMBED_URL_TEMPLATE.format(version=python_version)
    target = cache_dir / f"python-{python_version}-embed-amd64.zip"
    if target.is_file() and zipfile.is_zipfile(target):
        return target

    print(f"Downloading Windows embeddable Python from {url} ...")
    tmp = target.with_suffix(".zip.tmp")
    urllib.request.urlretrieve(url, tmp)
    if not zipfile.is_zipfile(tmp):
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded file from {url} is not a valid ZIP archive.")
    tmp.replace(target)
    return target


def _prune_staged_site_packages(site_packages: Path) -> None:
    for root, dirs, files in os.walk(site_packages, topdown=False):
        root_path = Path(root)
        for fname in files:
            if fname.endswith((".pyc", ".pyo")):
                (root_path / fname).unlink(missing_ok=True)
        for dname in dirs:
            dpath = root_path / dname
            if dname == "__pycache__" or (root_path == site_packages and dname == "bin"):
                shutil.rmtree(dpath, ignore_errors=True)


def stage_windows_bundle(
    *,
    stage_dir: Path,
    manifest_dir: Path,
    embed_zip: Path,
    python_minor: str,
    uv_bin: str = "uv",
    skip_pip: bool = False,
) -> None:
    """Assemble embeddable Python + Windows wheels + UTD client code + icon + NSIS manifests."""
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    if manifest_dir.exists():
        shutil.rmtree(manifest_dir)

    python_dir = stage_dir / "python"
    site_packages = python_dir / "site-packages"
    app_dir = python_dir / "app"
    python_dir.mkdir(parents=True, exist_ok=True)
    site_packages.mkdir(parents=True, exist_ok=True)
    app_dir.mkdir(parents=True, exist_ok=True)

    print(f"Extracting {embed_zip.name} into {python_dir} ...")
    with zipfile.ZipFile(embed_zip, "r") as zf:
        zf.extractall(python_dir)

    pth = patch_embed_pth(python_dir)
    print(f"Patched embeddable path file: {pth.name}")

    if not skip_pip:
        print(f"Cross-installing win_amd64 Python {python_minor} wheels via uv ...")
        subprocess.run(
            [
                uv_bin,
                "pip",
                "install",
                "--target",
                str(site_packages),
                "--python-platform",
                "x86_64-pc-windows-msvc",
                "--python-version",
                python_minor,
                "--only-binary=:all:",
                *CLIENT_RUNTIME_DEPS,
            ],
            check=True,
            cwd=str(_REPO_ROOT),
        )
        _prune_staged_site_packages(site_packages)

    # Copy `utd` client + shared + local backend modules (no Modal cloud deploy runtime required)
    src_utd = _REPO_ROOT / "src" / "utd"
    dst_utd = app_dir / "utd"
    shutil.copytree(
        src_utd,
        dst_utd,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "modal_app.py"),
    )

    write_ico(stage_dir / "app.ico")
    manifest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_REPO_ROOT / "LICENSE", manifest_dir / "LICENSE.txt")
    generate_nsis_manifests(stage_dir, manifest_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build-windows-installer",
        description="Cross-compile standalone Windows .exe installer on Linux using uv + NSIS.",
    )
    parser.add_argument("--python-version", default=DEFAULT_PYTHON_VERSION)
    parser.add_argument("--python-url", default=None, help="Custom URL for Windows embeddable ZIP.")
    parser.add_argument("--python-zip", type=Path, default=None, help="Local embeddable ZIP path.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .exe path (default: dist/UltimateTorrentDownloader-<ver>-win64-setup.exe).",
    )
    parser.add_argument(
        "--stage-only",
        action="store_true",
        help="Stage the Windows bundle and NSIS manifests without running makensis.",
    )
    parser.add_argument(
        "--skip-pip",
        action="store_true",
        help="Skip `uv pip install` (useful for testing staging offline).",
    )
    args = parser.parse_args(argv)

    uv_bin = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not args.skip_pip and not shutil.which(uv_bin):
        raise FileNotFoundError("`uv` binary was not found on PATH.")

    makensis_bin = shutil.which("makensis")
    if not args.stage_only and not makensis_bin:
        raise FileNotFoundError(
            "`makensis` (NSIS) was not found on PATH. Install it on Debian/Ubuntu with: "
            "sudo apt-get install nsis"
        )

    app_version = read_app_version()
    py_parts = args.python_version.split(".")
    python_minor = f"{py_parts[0]}.{py_parts[1]}"

    cache_dir = _REPO_ROOT / ".cache" / "windows-build"
    build_dir = _INSTALLER_DIR / "build"
    stage_dir = build_dir / "stage"
    manifest_dir = build_dir / "manifests"
    dist_dir = _REPO_ROOT / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    default_exe = dist_dir / f"UltimateTorrentDownloader-{app_version}-win64-setup.exe"
    output_exe = (args.output or default_exe).resolve()
    output_exe.parent.mkdir(parents=True, exist_ok=True)

    embed_zip = _download_embeddable_python(
        python_version=args.python_version,
        python_url=args.python_url,
        python_zip=args.python_zip,
        cache_dir=cache_dir,
    )

    stage_windows_bundle(
        stage_dir=stage_dir,
        manifest_dir=manifest_dir,
        embed_zip=embed_zip,
        python_minor=python_minor,
        uv_bin=uv_bin,
        skip_pip=args.skip_pip,
    )

    if args.stage_only:
        print(f"Staging complete at {stage_dir} (skipped makensis).")
        return 0

    assert makensis_bin is not None
    nsi_script = _INSTALLER_DIR / "installer.nsi"
    ver_parts = [int(p) if p.isdigit() else 0 for p in app_version.split(".")[:3]]
    while len(ver_parts) < 3:
        ver_parts.append(0)
    vi_version = f"{ver_parts[0]}.{ver_parts[1]}.{ver_parts[2]}.0"

    print(f"Compiling NSIS installer -> {output_exe} ...")
    subprocess.run(
        [
            makensis_bin,
            "-WX",
            f"-DAPP_VERSION={app_version}",
            f"-DAPP_VI_VERSION={vi_version}",
            f"-DSTAGE_DIR={stage_dir}",
            f"-DMANIFEST_DIR={manifest_dir}",
            f"-DOUTPUT_EXE={output_exe}",
            str(nsi_script),
        ],
        check=True,
        cwd=str(_REPO_ROOT),
    )

    digest = hashlib.sha256(output_exe.read_bytes()).hexdigest()
    sha_file = Path(f"{output_exe}.sha256")
    sha_file.write_text(f"{digest}  {output_exe.name}\n", encoding="utf-8")

    size_mb = output_exe.stat().st_size / (1024 * 1024)
    print(f"\nBuilt Windows installer: {output_exe} ({size_mb:.2f} MiB)")
    print(f"SHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
