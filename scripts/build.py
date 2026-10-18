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
import re
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


def _signing_identity() -> str | None:
    """The user's stable self-signed cert (Keychain Access -> Certificate
    Assistant -> Code Signing), matched by name. Signing with it keeps the
    app's code identity stable across rebuilds — an ad-hoc signature is the
    binary's content hash, so macOS Keychain treats every rebuild as a new
    app and re-asks for the credential password on every launch."""
    out = subprocess.run(
        ["security", "find-identity", "-p", "codesigning"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        # '  1) 448CC7F0... "ChatterSyn" (CSSMERR_TP_NOT_TRUSTED)'
        match = re.search(r'"([^"]+)"', line)
        if match and "chattersyn" in match.group(1).lower():
            return match.group(1)
    return None


_APP_INSTALL_TIP = (
    "tip: keep the .app out of iCloud-synced folders — the file provider can "
    "strip the bundle signature after the build (Keychain then re-prompts for "
    "passwords). e.g. ditto dist/chattersyn.app /Applications/chattersyn.app"
)


def main() -> int:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    if _run("icons", ["uv", "run", "python", "scripts/make_icon.py"], env):
        return 1
    if _run("pyinstaller", ["uv", "run", "pyinstaller", "--noconfirm", "chattersyn.spec"], env):
        return 1

    if sys.platform == "darwin":
        app = ROOT / "dist" / "chattersyn.app"
        # Sandboxed shells tag copied files with provenance xattrs that block
        # codesign ("resource fork, Finder information, or similar detritus");
        # clear them first (no-op on clean builds).
        subprocess.run(["xattr", "-cr", str(app)], check=False)
        identity = _signing_identity()
        if identity is not None:
            print(f"== codesign (identity: {identity})")
            if not subprocess.run(
                ["codesign", "--force", "--deep", "--sign", identity, str(app)],
                cwd=ROOT, env=env,
            ).returncode:
                # The iCloud file provider re-tags files under ~/Documents
                # with com.apple.provenance xattrs asynchronously after they
                # are written. Codesign's "detritus" hygiene check trips on
                # those, so strip them once more right after signing — they
                # do not affect the signature itself (content is unchanged).
                subprocess.run(["xattr", "-cr", str(app)], check=False)
                print(f"built {app}")
                print(_APP_INSTALL_TIP)
                return 0
            print("certificate signing failed — falling back to ad-hoc")
        if _run("codesign (ad-hoc)", ["codesign", "--force", "--deep", "--sign", "-", str(app)], env):
            return 1
        subprocess.run(["xattr", "-cr", str(app)], check=False)
        print(f"built {app}")
        print(_APP_INSTALL_TIP)
    else:
        print(f"built {ROOT / 'dist' / 'chattersyn'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
