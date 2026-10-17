"""EmoteRegistry: merges Twitch + 7TV + BTTV + FFZ emotes with configurable
priority, and serves the lookup callables TwitchMapper needs plus the scoped
queries the emote picker/completer need (Subs/Channel/Global tabs).

Emotes live in per-provider maps split by scope: channel-scoped (7TV user
sets, BTTV channel/shared, FFZ room) vs global (7TV global set, BTTV global,
FFZ global, Helix chat/emotes/global). ``lookup`` walks the configured
priority order and checks a provider's channel map before its global one, so
a channel emote still shadows a same-named global emote (BTTV parity).
"""

from __future__ import annotations

import asyncio
import logging

from ...config.settings import Settings
from ...models.emote import Emote
from ...services.twitch.mapper import twitch_emote_urls
from .bttv import BttvProvider
from .ffz import FfzProvider
from .provider import EmoteProvider
from .seventv import SevenTvProvider

log = logging.getLogger(__name__)


class EmoteRegistry:
    """Token → Emote resolution across providers, scope-aware for the picker."""

    def __init__(self, settings: Settings, theme: str = "dark"):
        self._settings = settings
        self._theme = theme
        self._priority: list[str] = list(
            settings.get("emote_providers.emote_priority", ["twitch", "7tv", "bttv", "ffz"])
        )
        self._channel: dict[str, dict[str, Emote]] = {}  # provider -> token -> Emote (loaded channel)
        self._global: dict[str, dict[str, Emote]] = {}   # provider -> token -> Emote (scope None)
        self._twitch_channel: dict[str, Emote] = {}      # Helix chat/emotes (subscriber sets)
        self._twitch_global: dict[str, Emote] = {}       # Helix chat/emotes/global
        self._twitch_names: dict[str, str] = {}          # channel emote id -> name
        self._providers: dict[str, EmoteProvider] = {
            "7tv": SevenTvProvider(),
            "bttv": BttvProvider(),
            "ffz": FfzProvider(),
        }
        self._loaded_channel: str | None = None
        self._global_loaded = False  # global sets are channel-independent: fetch once

    # -- loading ---------------------------------------------------------------------

    def set_twitch_emotes(self, id_to_name: dict[str, str]) -> None:
        """Twitch channel emotes from Helix chat/emotes ({emote_id: name})."""
        self._twitch_names = dict(id_to_name)
        self._twitch_channel = {
            name: Emote(
                id=eid,
                name=name,
                provider="twitch",
                urls=twitch_emote_urls(eid, self._theme),
                animated=False,
                scope=self._loaded_channel,
            )
            for eid, name in id_to_name.items()
            if name
        }

    def set_twitch_global_emotes(self, id_to_name: dict[str, str]) -> None:
        """Twitch global emotes from Helix chat/emotes/global (scope None)."""
        self._twitch_global = {
            name: Emote(
                id=eid,
                name=name,
                provider="twitch",
                urls=twitch_emote_urls(eid, self._theme),
                animated=False,
                scope=None,
            )
            for eid, name in id_to_name.items()
            if name
        }

    async def load_channel(self, channel_name: str, twitch_id: str) -> None:
        """Fetch all enabled third-party providers for a channel (best-effort).
        Global sets are fetched once on the first load and kept — they don't
        change per channel."""
        for key in list(self._channel):
            del self._channel[key]
        self._loaded_channel = channel_name
        tasks: list[tuple[str, str, object]] = []
        for name, provider in self._providers.items():
            if not self._settings.get(f"emote_providers.{name}", True):
                continue
            tasks.append((name, "channel", provider.fetch_for_channel(channel_name, twitch_id)))
            if not self._global_loaded:
                tasks.append((name, "global", provider.fetch_global()))
        results = await asyncio.gather(*[t for _, _, t in tasks], return_exceptions=True)
        for (name, kind, _), result in zip(tasks, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("%s %s emote fetch raised: %s", name, kind, result)
                continue
            target = self._channel if kind == "channel" else self._global
            target[name] = {e.name: e for e in result}
        self._global_loaded = True
        counts = {name: len(m) for name, m in {**self._channel, **self._global}.items()}
        log.info("Emotes for %s: %s", channel_name, counts)

    def clear_channel(self) -> None:
        self._loaded_channel = None
        self._channel.clear()
        self._global.clear()
        self._twitch_channel.clear()
        self._twitch_global.clear()
        self._twitch_names.clear()
        self._global_loaded = False

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()

    # -- lookups ----------------------------------------------------------------------

    def enabled(self, provider: str) -> bool:
        return bool(self._settings.get(f"emote_providers.{provider}", provider == "twitch"))

    def lookup(self, token: str) -> Emote | None:
        """First match in priority order (Twitch excluded — IRC emote tags are
        authoritative for Twitch emotes; third-party tokens only). A provider's
        channel map wins over its global map."""
        for provider in self._priority:
            if provider == "twitch" or not self.enabled(provider):
                continue
            emote = self._channel.get(provider, {}).get(token)
            if emote is None:
                emote = self._global.get(provider, {}).get(token)
            if emote is not None:
                return emote
        return None

    def name_for_twitch_id(self, emote_id: str) -> str | None:
        return self._twitch_names.get(emote_id)

    # -- picker/completer queries -------------------------------------------------------

    def all_emotes(self) -> list[Emote]:
        """Every known emote for the completer/favourites, deduped by token in
        priority order (lower-priority duplicates of a token are dropped)."""
        seen: dict[str, Emote] = {}
        for provider in self._priority:
            if not self.enabled(provider):
                continue
            for source in (self._global.get(provider, {}), self._channel.get(provider, {})):
                for token, emote in source.items():
                    seen.setdefault(token.lower(), emote)
        for source in (self._twitch_global, self._twitch_channel):
            for token, emote in source.items():
                seen.setdefault(token.lower(), emote)
        return list(seen.values())

    def sub_emotes(self) -> list[Emote]:
        """Subs tab: Twitch subscriber/access sets (Helix chat/emotes)."""
        return list(self._twitch_channel.values())

    def channel_emotes(self) -> list[Emote]:
        """Channel tab: channel-scoped third-party emotes."""
        out: list[Emote] = []
        for provider in self._priority:
            if provider == "twitch":
                continue
            out.extend(self._channel.get(provider, {}).values())
        return out

    def global_emotes(self) -> list[Emote]:
        """Global tab: Twitch globals + third-party globals."""
        out = list(self._twitch_global.values())
        for provider in self._priority:
            if provider == "twitch":
                continue
            out.extend(self._global.get(provider, {}).values())
        return out
