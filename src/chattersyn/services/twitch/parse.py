"""IRCv3 line/tag parsing for Twitch chat.

Splits a raw IRC line into tags / prefix / command / params, with IRCv3 tag
unescaping (\\s \\n \\r \\: \\\\) and the trailing-escaped-space rule. The
event-level mapping (PRIVMSG -> Message etc.) lives in mapper.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IrcMessage:
    command: str
    params: list[str] = field(default_factory=list)  # middle params + trailing
    prefix: str | None = None  # e.g. "forsen!forsen@forsen.tmi.twitch.tv"
    tags: dict[str, str] = field(default_factory=dict)
    raw: str = ""


def unescape_tag(value: str) -> str:
    """Unescape IRCv3 tag value sequences."""
    return (
        value.replace("\\:", ";")
        .replace("\\s", " ")
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\\\", "\\")
    )


def parse_tags(raw: str) -> dict[str, str]:
    """Parse the tag section of a message (without the leading '@')."""
    tags: dict[str, str] = {}
    pairs = raw.split(";")
    for index, pair in enumerate(pairs):
        key, sep, value = pair.partition("=")
        if not key:
            continue
        if not sep:
            tags[key] = ""
            continue
        value = unescape_tag(value)
        # IRCv3: if the last tag value ends with an escaped space, the
        # trailing space is stripped.
        if index == len(pairs) - 1 and value.endswith(" ") and raw.endswith("\\s"):
            value = value[:-1]
        tags[key] = value
    return tags


def parse_irc_line(line: str) -> IrcMessage | None:
    """Split a raw IRC line into an IrcMessage. Returns None for empty lines."""
    raw = line.rstrip("\r\n")
    if not raw:
        return None

    rest = raw
    tags: dict[str, str] = {}
    if rest.startswith("@"):
        tag_part, _, rest = rest[1:].partition(" ")
        tags = parse_tags(tag_part)

    prefix: str | None = None
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")

    params: list[str] = []
    if rest:
        command, _, middle = rest.partition(" ")
        if middle.startswith(":"):
            params = [middle[1:]]
        elif middle:
            head, sep, trailing = middle.partition(" :")
            params = head.split(" ")
            if sep:
                params.append(trailing)
    else:
        command = rest

    return IrcMessage(command=command, params=params, prefix=prefix, tags=tags, raw=raw)
