"""Platform-neutral message model. Both Twitch IRC and YouTube live chat map onto this."""

from __future__ import annotations

import datetime as dt
import enum
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .emote import Emote


class Platform(enum.Enum):
    TWITCH = "twitch"
    YOUTUBE = "youtube"
    SYSTEM = "system"


class MessageFlags(enum.IntFlag):
    """Drives row styling, mirroring Chatterino's MessageFlag bitset."""

    NONE = 0
    SYSTEM = 1 << 0
    ACTION = 1 << 1  # /me
    HIGHLIGHTED = 1 << 2
    SUBSCRIPTION = 1 << 3  # sub/resub/gift rows
    ANNOUNCEMENT = 1 << 4
    FIRST_MESSAGE = 1 << 5
    REPLY = 1 << 6
    DELETED = 1 << 7
    CHEER = 1 << 8  # bits
    SUPERCHAT = 1 << 9  # YouTube super chat
    MEMBERSHIP = 1 << 10  # YouTube membership events
    CENTERED = 1 << 11  # system-style centered row
    DISABLED = 1 << 12  # moderation mode: grayed-out row (timed out / locally hidden)


@dataclass
class Badge:
    set_id: str
    version_id: str
    title: str = ""
    tooltip: str = ""
    urls: dict[str, str] = field(default_factory=dict)  # {"1x": url, "2x": url, "4x": url}

    def url_for(self, dpr: float = 1.0) -> str | None:
        if dpr >= 2 and "2x" in self.urls:
            return self.urls["2x"]
        return self.urls.get("1x") or next(iter(self.urls.values()), None)


@dataclass
class Author:
    name: str
    login: str = ""
    user_id: str = ""
    color: str | None = None  # hex like "#ff0000" or None (default)
    badges: list[Badge] = field(default_factory=list)
    avatar_url: str | None = None


class Run:
    """Base class for inline message content."""


@dataclass
class TextRun(Run):
    text: str
    link: str | None = None  # set -> render as clickable link
    mention: str | None = None  # set -> render as clickable @mention (login)


@dataclass
class ImageRun(Run):
    emote: Emote
    alt: str = ""  # emote name, used for copy/paste + placeholder


@dataclass
class Message:
    id: str
    platform: Platform
    channel_id: str
    ts: dt.datetime
    author: Author | None  # None => system row
    runs: list[Run] = field(default_factory=list)
    plain: str = ""  # full plain text for copy/search/logging
    flags: MessageFlags = MessageFlags.NONE
    reply_parent: str | None = None
    reply_parent_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    arrived_at: float = field(default_factory=time.monotonic)

    # -- convenience constructors -------------------------------------------

    @classmethod
    def text(
        cls,
        platform: Platform,
        channel_id: str,
        author: Author | None,
        text: str,
        ts: dt.datetime | None = None,
        **kwargs: Any,
    ) -> Message:
        return cls(
            id=kwargs.pop("id", f"{platform.value}-{time.time_ns()}"),
            platform=platform,
            channel_id=channel_id,
            ts=ts or dt.datetime.now(),
            author=author,
            runs=[TextRun(text)],
            plain=text,
            **kwargs,
        )

    @classmethod
    def system(
        cls,
        platform: Platform,
        channel_id: str,
        text: str,
        ts: dt.datetime | None = None,
        **kwargs: Any,
    ) -> Message:
        kwargs.setdefault("flags", MessageFlags.SYSTEM | MessageFlags.CENTERED)
        return cls(
            id=kwargs.pop("id", f"sys-{time.time_ns()}"),
            platform=platform,
            channel_id=channel_id,
            ts=ts or dt.datetime.now(),
            author=None,
            runs=[TextRun(text)],
            plain=text,
            **kwargs,
        )

    @property
    def is_system(self) -> bool:
        return self.author is None or bool(self.flags & MessageFlags.SYSTEM)
