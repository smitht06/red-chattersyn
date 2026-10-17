"""Autocomplete candidate building — pure, Qt-free logic shared by the
completion popup, Tab cycling and the tests.

Chatterino parity (docs/m4.md "Autocomplete"): the POPUP serves `:` emotes
(case-insensitive contains, exact match hoisted, `~` = zero-width only) and
emojis, plus `@` users (prefix-only); it never serves `/`-commands. TAB
cycling adds first-word `/`/`.`→commands, first-word unified emote+user+
command and mid-message emote+user matching (start-of-word for emotes).
A reply prefix (`@Name `) is ignored for first-word detection. Hard cap
200 candidates.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.emote import Emote
from .emojis import EmojiEntry

MAX_CANDIDATES = 200

_COMMAND_SIGILS = ("/", ".")


@dataclass(frozen=True)
class CompletionCandidate:
    name: str                 # displayed name
    insert: str               # replaces the typed word (trailing space)
    sub: str = ""             # provider label / "user" / "command" / "emoji"
    emote: Emote | None = None  # thumbnail source (emote rows)
    glyph: str = ""           # emoji char (emoji rows)


@dataclass(frozen=True)
class CompletionResult:
    start: int                # word start index in the ORIGINAL text
    end: int                  # word end index in the ORIGINAL text
    candidates: list[CompletionCandidate]
    popup: bool               # popup-eligible context (@ or :)


def word_bounds(text: str, cursor: int) -> tuple[int, int]:
    """(start, end) of the whitespace-delimited word the cursor sits in;
    (cursor, cursor) when the cursor is not inside a word."""
    if cursor <= 0 or cursor > len(text) or text[cursor - 1].isspace():
        return cursor, cursor
    start = cursor - 1
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    end = cursor
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def match_users(prefix: str, names: list[str]) -> list[str]:
    """Prefix-only, case-insensitive; first occurrence of each name wins."""
    needle = prefix.lower()
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.lower()
        if key not in seen and key.startswith(needle):
            seen.add(key)
            out.append(name)
    return out


def match_emotes(prefix: str, emotes: list[Emote]) -> list[Emote]:
    """Case-insensitive contains; exact match hoisted to index 0; a `~`
    prefix restricts to zero-width emotes."""
    zero_only = prefix.startswith("~")
    needle = prefix.lstrip("~").lower()
    by_name: dict[str, Emote] = {}
    for emote in emotes:
        if zero_only and not emote.zero_width:
            continue
        key = emote.name.lower()
        if needle in key and key not in by_name:
            by_name[key] = emote
    return sorted(
        by_name.values(),
        key=lambda e: (e.name.lower() != needle, e.name.lower()),
    )


def match_emojis(prefix: str, entries: tuple[EmojiEntry, ...] | list[EmojiEntry]) -> list[EmojiEntry]:
    """Shortcode contains (ci), exact match hoisted (same rules as emotes)."""
    needle = prefix.lower()
    return sorted(
        (e for e in entries if needle in e.shortcode.lower()),
        key=lambda e: (e.shortcode.lower() != needle, e.shortcode.lower()),
    )


def match_commands(prefix: str, commands: list[str]) -> list[str]:
    """Prefix-only, ci, after stripping a leading / or . from the command."""
    needle = prefix.lower()
    stripped = {c.lstrip("/.") for c in commands}
    return sorted((c for c in stripped if c.lower().startswith(needle)), key=str.lower)


def build_candidates(
    text: str,
    cursor: int,
    *,
    users: list[str],
    emotes: list[Emote],
    commands: list[str],
    emojis: tuple[EmojiEntry, ...] | list[EmojiEntry],
    reply_prefix: str = "",
) -> CompletionResult | None:
    """Candidates for the word under the cursor, or None (no completion).

    `reply_prefix` (the '@Name ' banner prefix) is stripped before first-word
    detection, and returned indices refer to the original text.
    """
    offset = 0
    if reply_prefix and text.startswith(reply_prefix):
        if cursor < len(reply_prefix):
            return None  # typing inside the reply prefix itself
        offset = len(reply_prefix)
        text = text[offset:]
        cursor -= offset

    start, end = word_bounds(text, cursor)
    if start == end:
        return None
    word = text[start:end]
    prefix = text[start:cursor]
    first_word = start == 0
    sigil = word[0] if word[0] in _COMMAND_SIGILS or word[0] in "@:" else ""

    if sigil == "@":
        candidates = [
            CompletionCandidate(name=name, insert=f"@{name} ", sub="user")
            for name in match_users(prefix[1:], users)
        ]
        popup = True
    elif sigil == ":":
        candidates = [
            *[
                CompletionCandidate(name=e.name, insert=f"{e.name} ", sub=e.provider, emote=e)
                for e in match_emotes(prefix[1:], emotes)
            ],
            *[
                CompletionCandidate(name=e.shortcode, insert=f":{e.shortcode}: ",
                                    sub="emoji", glyph=e.char)
                for e in match_emojis(prefix[1:], emojis)
            ],
        ]
        popup = True
    elif sigil in _COMMAND_SIGILS and first_word:
        candidates = [
            CompletionCandidate(name=f"{sigil}{name}", insert=f"{sigil}{name} ",
                                sub="command")
            for name in match_commands(prefix[1:], commands)
        ]
        popup = False
    else:
        candidates = _plain_word_candidates(
            prefix, users, emotes, commands, include_commands=first_word)
        popup = False

    if not candidates:
        return None
    return CompletionResult(start=start + offset, end=end + offset,
                            candidates=candidates[:MAX_CANDIDATES], popup=popup)


def _plain_word_candidates(prefix: str, users: list[str], emotes: list[Emote],
                           commands: list[str], *, include_commands: bool) -> list[CompletionCandidate]:
    """Tab-cycle matching: start-of-word emotes/users (+ commands when the
    word is first in the message)."""
    needle = prefix.lower()
    candidates: list[CompletionCandidate] = []
    for emote in sorted(emotes, key=lambda e: e.name.lower()):
        if emote.name.lower().startswith(needle):
            candidates.append(
                CompletionCandidate(name=emote.name, insert=f"{emote.name} ",
                                    sub=emote.provider, emote=emote))
    for name in match_users(prefix, users):
        candidates.append(CompletionCandidate(name=name, insert=f"@{name} ", sub="user"))
    if include_commands:
        for name in match_commands(prefix, commands):
            candidates.append(
                CompletionCandidate(name=name, insert=f"/{name} ", sub="command"))
    return candidates
