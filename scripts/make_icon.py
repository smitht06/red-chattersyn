"""Derive the PyInstaller icon inputs from the ChatterSyn logo.

packaging/icon.png (1024x1024, committed) is rendered from the master logo
packaging/icon.svg by scripts/replace_logo.py (the /replace-logo skill), so
don't edit it by hand. This writes packaging/icon.icns (macOS .app) and
packaging/icon.ico (Windows .exe) from it; the Linux build uses the PNG
directly (chattersyn.spec, build_deb.py). CI and scripts/build.py run it
before PyInstaller.

Pillow only — no Qt, so it runs in the sandboxed dev shell too.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging"
MASTER = OUT / "icon.png"
SIZES = [16, 32, 64, 128, 256, 512, 1024]


def main() -> int:
    with Image.open(MASTER) as opened:
        base = opened.convert("RGBA")
    if base.width != base.height:
        print(f"error: {MASTER} must be square, got {base.width}x{base.height}", file=sys.stderr)
        return 1
    if base.width < SIZES[-1]:
        print(
            f"warning: {MASTER} is {base.width}px; the large macOS sizes get upscaled "
            f"(use {SIZES[-1]}x{SIZES[-1]})",
            file=sys.stderr,
        )

    sized = [base.resize((s, s), Image.Resampling.LANCZOS) for s in SIZES]
    base.save(OUT / "icon.icns", format="ICNS", append_images=sized)
    base.save(OUT / "icon.ico", format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {OUT / 'icon.icns'} and {OUT / 'icon.ico'} from {MASTER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
