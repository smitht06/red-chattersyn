"""Local packaging script: icons -> PyInstaller -> (macOS) ad-hoc codesign.

Usage::

    uv sync --group build   # once, to install pyinstaller + pillow
    uv run python scripts/build.py

Outputs dist/chattersyn.app (macOS) or dist/chattersyn/ (Windows/Linux).
Mirrors .github/workflows/build.yml so local builds match CI output.

Robust against the sandboxed dev shell's UF_HIDDEN quirk: the venv self-heal
runs first (best effort), and the icon step needs no Qt platform plugin.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "scripts"))
from _venv_fix import unhide_venv_files  # noqa: E402

unhide_venv_files()


def _run(name: str, cmd: list[str], env: dict[str, str]) -> int:
    print(f"== {name}")
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode


def main() -> int:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    if _run("icons", ["uv", "run", "python", "scripts/make_icon.py"], env):
        return 1
    if _run("pyinstaller", ["uv", "run", "pyinstaller", "--noconfirm", "chattersyn.spec"], env):
        return 1

    if sys.platform == "darwin":
        app = ROOT / "dist" / "chattersyn.app"
        # Sandboxed shells tag copied files with provenance xattrs that block
        # codesign; clear them first (no-op on clean builds).
        subprocess.run(["xattr", "-cr", str(app)], check=False)
        if _run("codesign (ad-hoc)", ["codesign", "--force", "--deep", "--sign", "-", str(app)], env):
            return 1
        print(f"built {app}")
    else:
        print(f"built {ROOT / 'dist' / 'chattersyn'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
