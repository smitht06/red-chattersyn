"""7TV v3 emote provider (7tv.io/v3)."""

from __future__ import annotations

import logging

from ...models.emote import Emote
from .provider import EmoteProvider

log = logging.getLogger(__name__)

API = "https://7tv.io/v3"
CDN = "https://cdn.7tv.app"


class SevenTvProvider(EmoteProvider):
    name = "7tv"

    async def fetch_for_channel(self, channel_name: str, twitch_id: str) -> list[Emote]:
        try:
            response = await self.client.get(f"{API}/users/twitch/{twitch_id}")
            response.raise_for_status()
            user = response.json()
        except Exception as exc:  # best-effort provider
            log.warning("7TV user lookup failed for %s: %s", channel_name, exc)
            return []

        set_id = self._set_id(user)
        if not set_id:
            return []
        emotes = await self._fetch_set(set_id, channel_name)
        log.debug("7TV: %d emotes for %s", len(emotes), channel_name)
        return emotes

    async def fetch_global(self) -> list[Emote]:
        """The public global emote set (7tv.io/v3/emote-sets/global)."""
        return await self._fetch_set("global", None)

    async def _fetch_set(self, set_id: str, scope: str | None) -> list[Emote]:
        try:
            response = await self.client.get(f"{API}/emote-sets/{set_id}")
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            log.warning("7TV emote-set %s fetch failed: %s", set_id, exc)
            return []

        emotes: list[Emote] = []
        for item in data.get("emotes", []):
            eid = str(item.get("id", ""))
            name = item.get("name", "")
            info = item.get("data") or {}
            if not eid or not name:
                continue
            host = info.get("host") or {}
            host_url = str(host.get("url", CDN)).removeprefix("//")
            urls: dict[str, str] = {}
            for f in host.get("files", []):
                fname = str(f.get("name", ""))
                if fname.endswith(".webp") and fname[0].isdigit():
                    scale = fname.split(".")[0]
                    urls[scale] = f"https://{host_url}/{fname}"
            if not urls:
                urls = {"1x": f"https://{host_url}/emote/{eid}/1x.webp"}
            emotes.append(
                Emote(
                    id=eid,
                    name=name,
                    provider="7tv",
                    urls=urls,
                    animated=bool(info.get("animated", False)),
                    zero_width=bool(int(item.get("flags", 0)) & 1),
                    height_px=self._height(info),
                    scope=scope,
                )
            )
        return emotes

    @staticmethod
    def _set_id(user: dict) -> str:
        """The user's active emote set id, wherever v3 decides to put it."""
        active = user.get("emote_set") or {}
        if isinstance(active, dict) and active.get("id"):
            return str(active["id"])
        sets = user.get("emote_sets") or []
        if isinstance(sets, list) and sets and sets[0].get("id"):
            return str(sets[0]["id"])
        return str(user.get("emote_set_id", "") or "")

    @staticmethod
    def _height(info: dict) -> int:
        # Prefer the 1x file's height; the host may not include sizes.
        host = info.get("host") or {}
        files = host.get("files") or []
        for f in files:
            if f.get("name") == "1x.webp" and f.get("height"):
                return int(f["height"])
        return 28
