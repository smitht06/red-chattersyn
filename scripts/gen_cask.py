#!/usr/bin/env python3
"""Generate the Homebrew cask for one macOS release from the template.

Usage::

    gen_cask.py <version> <sha256> <out-dir>

Substitutes __VERSION__ / __SHA256__ in
packaging/homebrew/Casks/chattersyn.rb and writes Casks/chattersyn.rb plus
README.md under <out-dir>. Mirrors build_deb.py's control-template
substitution. The sha256 must be the digest of the *remote* release asset —
the exact bytes brew will download — never a fresh local rebuild (rebuilds
are not byte-identical and brew would fail the checksum verification).
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "packaging" / "homebrew"


def generate(version: str, sha256: str, out_dir: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256, re.IGNORECASE):
        raise ValueError(f"invalid sha256: {sha256!r}")
    template = (TEMPLATE_DIR / "Casks" / "chattersyn.rb").read_text()
    cask = template.replace("__VERSION__", version).replace("__SHA256__", sha256)
    if "__" in cask:
        raise ValueError("template placeholders remain after substitution")
    casks_dir = out_dir / "Casks"
    casks_dir.mkdir(parents=True, exist_ok=True)
    (casks_dir / "chattersyn.rb").write_text(cask)
    shutil.copy2(TEMPLATE_DIR / "README.md", out_dir / "README.md")


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        generate(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"generated cask {sys.argv[1]} into {sys.argv[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
