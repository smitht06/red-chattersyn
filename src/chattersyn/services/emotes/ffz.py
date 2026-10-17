"""FrankerFaceZ v1 emote provider (api.frankerfacez.com/v1/room)."""

from __future__ import annotations

import logging

from ...models.emote import Emote
from .provider import EmoteProvider

log = logging.getLogger(__name__)

API = "https://api.frankerfacez.com/v1"

# FFZ modifier_flags: 1=hidden (zero-width) 2=flip 4=rainbow 8=hyper.
# Only plain (0) and zero-width (1) emotes participate in token matching.
_MATCHABLE = (0, 1)
_SCALE_KEYS = {"1": "1x", "2": "2x", "4": "4x"}


class FfzProvider(EmoteProvider):
    name = "ffz"

    async def fetch_for_channel(self, channel_name: str, twitch_id: str) -> list[Emote]:
        try:
            response = await self.client.get(f"{API}/room/{channel_name}")
            if response.status_code == 404:
                return []  # channel has no FFZ set — normal
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            log.warning("FFZ room lookup failed for %s: %s", channel_name, exc)
            return []

        room = data.get("room") or {}
        default_set = room.get("set")
        if default_set is None:
            return []
        emoticons = (data.get("sets") or {}).get(str(default_set), {}).get("emoticons", [])
        emotes = self._emotes_from(emoticons, scope=channel_name)
        log.debug("FFZ: %d emotes for %s", len(emotes), channel_name)
        return emotes

    async def fetch_global(self) -> list[Emote]:
        """The global FFZ set (v1/set/global); emoticons sit directly under
        'set' there, unlike the room response's sets map."""
        try:
            response = await self.client.get(f"{API}/set/global")
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            log.warning("FFZ global set fetch failed: %s", exc)
            return []
        top = data.get("set") or {}
        emoticons = top.get("emoticons") or []
        if not emoticons:  # tolerate the room-style shape too
            emoticons = (data.get("sets") or {}).get(str(top.get("id", "")), {}).get(
                "emoticons", [])
        return self._emotes_from(emoticons)

    @staticmethod
    def _emotes_from(emoticons: list[dict], scope: str | None = None) -> list[Emote]:
        emotes: list[Emote] = []
        for item in emoticons:
            eid = str(item.get("id", ""))
            name = item.get("name", "")
            flags = int(item.get("modifier_flags", 0))
            if not eid or not name or flags not in _MATCHABLE:
                continue
            urls: dict[str, str] = {}
            for key, value in (item.get("urls") or {}).items():
                scale = _SCALE_KEYS.get(str(key))
                if scale and isinstance(value, str):
                    urls[scale] = "https:" + value if value.startswith("//") else value
            if not urls:
                continue
            emotes.append(
                Emote(
                    id=eid,
                    name=name,
                    provider="ffz",
                    urls=urls,
                    animated=False,  # v1 is all static PNG
                    zero_width=flags == 1,
                    height_px=int(item.get("height", 28)),
                    scope=scope,
                    modifier_flags=flags,
                )
            )
        return emotes
