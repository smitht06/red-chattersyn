"""Regenerate src/chattersyn/assets/emoji.json from the `emoji` package.

The dataset is BUNDLED (committed as emoji.json) so the app has zero runtime
dependency on the `emoji` package; this script exists to refresh it:

    uv run python scripts/gen_emoji_data.py

Categories are approximated from Unicode block ranges (CLDR categories aren't
in the package); the "Component" category (skin tones) is sorted last, as in
Chatterino's emote picker.
"""

from __future__ import annotations

import json
from pathlib import Path

import emoji

OUT = Path(__file__).resolve().parent.parent / "src/chattersyn/assets/emoji.json"


def category_of(char: str) -> str:
    cp = next((ord(c) for c in char if not 0xFE00 <= ord(c) <= 0xFE0F), ord(char[0]))
    if 0x1F3FB <= cp <= 0x1F3FF:
        return "Component"
    if 0x1F1E6 <= cp <= 0x1F1FF:
        return "Flags"
    if 0x1F600 <= cp <= 0x1F64F or 0x1F920 <= cp <= 0x1F92F:
        return "Smileys & Emotion"
    if 0x1F300 <= cp <= 0x1F32F or 0x1F680 <= cp <= 0x1F6FF:
        return "Travel & Places"
    if 0x1F330 <= cp <= 0x1F34F or 0x1F400 <= cp <= 0x1F43F or 0x1F980 <= cp <= 0x1F9AF:
        return "Animals & Nature"
    if 0x1F350 <= cp <= 0x1F37F or 0x1F950 <= cp <= 0x1F96F or 0x1F9C0 <= cp <= 0x1F9CF:
        return "Food & Drink"
    if (0x1F900 <= cp <= 0x1F93F or 0x1F9B0 <= cp <= 0x1F9BF
            or 0x1F9D0 <= cp <= 0x1F9DF):
        return "People & Body"
    if 0x1F3C0 <= cp <= 0x1F3FA or 0x1F940 <= cp <= 0x1F94F:
        return "Activities"
    if 0x1F380 <= cp <= 0x1F3BF or 0x1F5FB <= cp <= 0x1F5FF or 0x1F970 <= cp <= 0x1F97F \
            or 0x1F9E0 <= cp <= 0x1F9FF or 0x1FA70 <= cp <= 0x1FAFF \
            or 0x1FA00 <= cp <= 0x1FA6F:
        return "Objects"
    return "Symbols"  # misc symbols, dingbats, arrows, hearts, keys, enclosed


def main() -> None:
    # EMOJI_DATA is keyed by CHARACTER; values carry the ":shortcode:" in "en"
    # and a status (1 fully-qualified, 2 minimally-qualified, 4 component).
    by_char: dict[str, list] = {}
    for char, data in emoji.EMOJI_DATA.items():
        status = data.get("status", 1)
        if status not in (1, 2, 4):
            continue
        en = data.get("en", "")
        if not (en.startswith(":") and en.endswith(":")):
            continue
        name = en[1:-1]
        if not name:
            continue
        entry = [name, char, category_of(char), status]
        current = by_char.get(char)
        if current is None or status < current[3]:
            by_char[char] = entry  # prefer the fully-qualified variant
    entries = sorted((e[:3] for e in by_char.values()), key=lambda e: e[0])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(entries, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"wrote {len(entries)} emoji to {OUT}")


if __name__ == "__main__":
    main()
