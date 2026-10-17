"""Text utilities: Twitch codepoint/UTF-16 handling, URL detection, emote tokenizing."""

from __future__ import annotations

import re

TWITCH_MESSAGE_LIMIT = 500  # codepoints
YOUTUBE_MESSAGE_LIMIT = 200  # characters

URL_RE = re.compile(r"(?i)\b((?:https?://|www\.)[^\s<>\"')\]]+)")
PUNCT_STRIP = set(".,!?;:()[]{}<>\"'")

_TOKEN_RE = re.compile(r"\S+")


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units (what Twitch's emotes tag indices count)."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def utf16_to_py_map(text: str) -> list[int]:
    """Map UTF-16 unit index -> Python str index (list of length utf16_len(text)+1)."""
    mapping = [0]
    for i, ch in enumerate(text):
        width = 1 + (ord(ch) > 0xFFFF)  # astral chars = surrogate pair = 2 units
        for _ in range(width):
            mapping.append(i + 1)
    return mapping


def utf16_range_to_py(text: str, start_units: int, end_units: int) -> tuple[int, int]:
    """Convert a half-open UTF-16 unit range [start, end) to Python str indices.

    Twitch's `emotes` tag gives zero-based, *inclusive* end indices in UTF-16
    units; callers pass end+1 here.
    """
    mapping = utf16_to_py_map(text)
    total = len(mapping) - 1
    py_start = mapping[min(start_units, total)]
    py_end = mapping[min(end_units, total)]
    return py_start, py_end


def parse_emotes_tag(text: str, tag: str) -> list[tuple[str, int, int]]:
    """Parse an IRC `emotes` tag into (emote_id, py_start, py_end) triples.

    Tag indices are zero-based UTF-16 code units, END-INCLUSIVE, relative to
    the FULL message text — including the CTCP ACTION prefix (`\\x01ACTION `)
    for /me messages. Returns half-open Python str indices into the same full
    text. Callers that strip the ACTION wrapper must shift the ranges back.
    """
    if not tag:
        return []
    mapping = utf16_to_py_map(text)
    total = len(mapping) - 1

    result: list[tuple[str, int, int]] = []
    for entry in tag.split("/"):
        emote_id, _, positions = entry.partition(":")
        if not positions:
            continue
        for span in positions.split(","):
            start_s, _, end_s = span.partition("-")
            if not end_s:
                continue
            py_start = mapping[min(int(start_s), total)]
            # Twitch end indices are inclusive in UTF-16 units.
            py_end = mapping[min(int(end_s) + 1, total)]
            result.append((emote_id, py_start, py_end))
    result.sort(key=lambda t: (t[1], t[2]))
    return result


def truncate_codepoints(text: str, limit: int) -> str:
    """Truncate to `limit` codepoints (len() counts codepoints in Python)."""
    return text if len(text) <= limit else text[:limit]


def prepare_twitch_send(text: str) -> tuple[str, bool]:
    """Sanitize for IRC and resolve the /me ACTION prefix.

    Returns (payload, is_action). Shared by IRC.send and the SendCoordinator
    so the optimistic message shows exactly what goes on the wire.
    """
    text = " ".join(text.replace("\x00", "").splitlines())
    is_action = text.startswith("/me ") and len(text) > 4
    body = text[4:].lstrip() if is_action else text
    return body, is_action


def contains_url(text: str) -> bool:
    return URL_RE.search(text) is not None


def iter_urls(text: str) -> list[str]:
    return [match.group(1) for match in URL_RE.finditer(text)]


def emote_tokens(text: str) -> list[str]:
    """Whitespace-delimited tokens for third-party emote matching.

    Tries the raw token, then the token with trailing sentence punctuation
    stripped (so `KEKW,` still matches `KEKW`; leading punctuation like `:)`
    is kept since FFZ/BTTV emotes are often pure punctuation).
    """
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        if raw in tokens:
            continue
        tokens.append(raw)
        stripped = raw.rstrip("".join(PUNCT_STRIP))
        if stripped and stripped != raw and stripped not in tokens:
            tokens.append(stripped)
    return tokens
