"""Emote model shared by Twitch and third-party providers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Emote:
    """A chat emote from Twitch, 7TV, BTTV or FFZ."""

    id: str
    name: str
    provider: str  # "twitch" | "7tv" | "bttv" | "ffz"
    urls: dict[str, str] = field(default_factory=dict)  # {"1x": url, "2x": url, ...}
    animated: bool = False
    zero_width: bool = False
    height_px: int = 28
    scope: str | None = None  # None = global, else channel it belongs to
    modifier_flags: int = 0  # FFZ modifier_flags: 1=hidden/zero-width 2=flip 4=rainbow 8=hyper

    @property
    def url(self) -> str | None:
        """Best single URL (prefer 1x)."""
        return self.urls.get("1x") or next(iter(self.urls.values()), None)

    def url_for_scale(self, scale: str) -> str | None:
        return self.urls.get(scale) or self.url
