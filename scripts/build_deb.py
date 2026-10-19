"""Assemble and build the Linux .deb from the PyInstaller onedir bundle.

Usage::

    uv sync --group build   # once, to install pyinstaller + pillow
    uv run pyinstaller --noconfirm chattersyn.spec
    uv run python scripts/build_deb.py

Requires ``dist/chattersyn`` (the PyInstaller output) and ``dpkg-deb`` on a
Debian/Ubuntu machine. Mirrors the CI Linux leg.

Package layout::

    /opt/chattersyn/...         the frozen bundle
    /usr/bin/chattersyn         symlink to /opt/chattersyn/chattersyn
    /usr/share/applications/    desktop entry
    /usr/share/icons/hicolor/   sized app icons (from packaging/icon.png)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HICOLOR_SIZES = (512, 256, 128, 64, 48)


def deb_path(dist: Path, version: str) -> Path:
    """Release artifact name, matching the CI upload glob ``dist/chattersyn-*``."""
    return dist / f"chattersyn-{version}-linux-x86_64.deb"


def build_staging(dist: Path, version: str, arch: str, staging: Path, *, icons: bool = True) -> None:
    """Assemble the dpkg staging tree. Pure Python so tests can run without dpkg;
    pass ``icons=False`` when Pillow is unavailable."""
    bundle = dist / "chattersyn"
    binary = bundle / "chattersyn"
    if not binary.is_file():
        raise FileNotFoundError(f"{binary} missing — build with PyInstaller first")
    if staging.exists():
        shutil.rmtree(staging)

    debian = staging / "DEBIAN"
    debian.mkdir(parents=True)
    control = (ROOT / "packaging" / "linux" / "control").read_text()
    (debian / "control").write_text(
        control.replace("__VERSION__", version).replace("__ARCH__", arch))

    shutil.copytree(bundle, staging / "opt" / "chattersyn")

    bin_dir = staging / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "chattersyn").symlink_to("/opt/chattersyn/chattersyn")

    apps = staging / "usr" / "share" / "applications"
    apps.mkdir(parents=True)
    shutil.copy2(ROOT / "packaging" / "linux" / "chattersyn.desktop", apps)

    if icons:
        from PIL import Image  # build-group dependency, not in the dev group

        master = Image.open(ROOT / "packaging" / "icon.png")
        for size in HICOLOR_SIZES:
            out_dir = staging / "usr" / "share" / "icons" / "hicolor" / f"{size}x{size}" / "apps"
            out_dir.mkdir(parents=True)
            master.resize((size, size), Image.LANCZOS).save(out_dir / "chattersyn.png")


def main() -> int:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    dist = ROOT / "dist"
    staging = dist / "deb-staging"
    build_staging(dist, version, "amd64", staging)
    out = deb_path(dist, version)
    out.unlink(missing_ok=True)
    print("== dpkg-deb")
    ret = subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(staging), str(out)], cwd=ROOT
    ).returncode
    if ret == 0:
        print(f"built {out}")
    return ret


if __name__ == "__main__":
    sys.exit(main())
