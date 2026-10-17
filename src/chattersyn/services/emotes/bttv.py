"""BetterTTV v3 emote provider (api.betterttv.net/3/cached)."""

from __future__ import annotations

import logging

from ...models.emote import Emote
from .provider import EmoteProvider

log = logging.getLogger(__name__)

API = "https://api.betterttv.net/3/cached"
CDN = "https://cdn.betterttv.net/emote/{id}/{scale}"


class BttvProvider(EmoteProvider):
    name = "bttv"

    async def fetch_for_channel(self, channel_name: str, twitch_id: str) -> list[Emote]:
        """Channel + shared emotes only, tagged with the channel scope (the
        picker needs the channel/global distinction preserved)."""
        try:
            response = await self.client.get(f"{API}/users/twitch/{twitch_id}")
            if response.status_code == 404:
                return []  # most channels have no BTTV account — normal
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            log.warning("BTTV user lookup failed for %s: %s", twitch_id, exc)
            return []
        return self._emotes_from(
            data.get("channelEmotes", []) + data.get("sharedEmotes", []),
            scope=channel_name,
        )

    async def fetch_global(self) -> list[Emote]:
        try:
            response = await self.client.get(f"{API}/emotes/global")
            response.raise_for_status()
        except Exception as exc:
            log.warning("BTTV global emote fetch failed: %s", exc)
            return []
        return self._emotes_from(response.json())

    @staticmethod
    def _emotes_from(items: list[dict], scope: str | None = None) -> list[Emote]:
        emotes: list[Emote] = []
        for item in items:
            eid = str(item.get("id", ""))
            code = item.get("code", "")
            if not eid or not code:
                continue
            emotes.append(
                Emote(
                    id=eid,
                    name=code,
                    provider="bttv",
                    urls={scale: CDN.format(id=eid, scale=scale) for scale in ("1x", "2x", "3x")},
                    animated=bool(item.get("animated", False)),
                    scope=scope,
                )
            )
        return emotes
