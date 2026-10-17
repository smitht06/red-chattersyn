"""Channel reference model."""

from __future__ import annotations

from dataclasses import dataclass

from .message import Platform


@dataclass
class ChannelRef:
    """A chat endpoint: Twitch channel or YouTube live chat."""

    platform: Platform
    id: str  # Twitch: login name; YouTube: channel id
    display_name: str = ""
    live_chat_id: str | None = None  # YouTube only
    live: bool = False
    title: str | None = None
    category: str | None = None
    viewer_count: int | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.id
