#!/usr/bin/env python3
"""Put one release's notes in the "Latest release" block at the bottom of
README.md.

Usage::

    update_readme_release.py <version> <date> <notes.md> [README.md]

<version> is the tag (``v0.6.0``), <date> its ISO date (``2026-10-26``) and
<notes.md> the Markdown body (no heading). The block lives between
``<!-- release-notes:start -->`` / ``<!-- release-notes:end -->`` markers and
is REPLACED each time — the README only ever carries the most recent release
(older ones live on the GitHub Releases page). A README without the markers
gets the block appended at the end. Written by the release-notes skill
(.claude/skills/release-notes/SKILL.md).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START = "<!-- release-notes:start -->"
END = "<!-- release-notes:end -->"
RELEASES_URL = "https://github.com/smitht06/red-chattersyn/releases"


def render_block(version: str, date: str, notes: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise ValueError(f"invalid version tag: {version!r} (expected vX.Y.Z)")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError(f"invalid date: {date!r} (expected YYYY-MM-DD)")
    body = notes.strip()
    if not body:
        raise ValueError("release notes are empty")
    if START in body or END in body:
        raise ValueError("release notes must not contain the README markers")
    return (
        f"{START}\n"
        f"## Latest release: {version} ({date})\n\n"
        f"{body}\n\n"
        f"Older releases and downloads: [GitHub Releases]({RELEASES_URL}).\n"
        f"{END}\n"
    )


def update(readme: str, block: str) -> str:
    """`readme` with its release block replaced (or appended at the end)."""
    start, end = readme.find(START), readme.find(END)
    if start == -1 and end == -1:
        return readme.rstrip("\n") + "\n\n" + block
    if start == -1 or end == -1 or end < start:
        raise ValueError("README has a broken release-notes marker pair")
    return readme[:start] + block + readme[end + len(END):].lstrip("\n")


def main() -> int:
    if len(sys.argv) not in (4, 5):
        print(__doc__, file=sys.stderr)
        return 2
    version, date, notes_path = sys.argv[1:4]
    readme_path = Path(sys.argv[4]) if len(sys.argv) == 5 else ROOT / "README.md"
    try:
        block = render_block(version, date, Path(notes_path).read_text(encoding="utf-8"))
        readme_path.write_text(
            update(readme_path.read_text(encoding="utf-8"), block), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"README release block -> {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
