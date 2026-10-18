"""EmoteRegistry: merges Twitch + 7TV + BTTV + FFZ emotes with configurable
priority, and serves the lookup callables TwitchMapper needs plus the scoped
queries the emote picker/completer need (Subs/Channel/Global tabs).

Emotes live in per-provider maps split by scope: channel-scoped (7TV user
sets, BTTV channel/shared, FFZ room, Helix chat/emotes) vs global (7TV global
set, BTTV global, FFZ global, Helix chat/emotes/global). Channel-scoped maps
are kept PER CHANNEL so parallel Twitch tabs each resolve their own emotes;
``lookup``/``lookup_by_name`` take an optional channel and check that
channel's map before the global one, so a channel emote still shadows a
same-named global emote (BTTV parity). Without a channel they use the active
(last-loaded) channel, which is also what the picker/completer views.
"""

from __future__ import annotations

import asyncio
import logging
import re

from ...config.settings import Settings
from ...core.text import PUNCT_STRIP
from ...models.emote import Emote
from ...services.twitch.mapper import twitch_emote_urls
from .bttv import BttvProvider
from .ffz import FfzProvider
from .provider import EmoteProvider
from .seventv import SevenTvProvider

# Emote names are [A-Za-z0-9_]+; everything else in a message is a separator.
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

log = logging.getLogger(__name__)


class EmoteRegistry:
    """Token → Emote resolution across providers, scope-aware for the picker."""

    def __init__(self, settings: Settings, theme: str = "dark"):
        self._settings = settings
        self._theme = theme
        self._priority: list[str] = list(
            settings.get("emote_providers.emote_priority", ["twitch", "7tv", "bttv", "ffz"])
        )
        # Per-channel stores (channel names are normalized: lowercased, no #).
        self._channels: dict[str, dict[str, dict[str, Emote]]] = {}  # channel -> provider -> token -> Emote
        self._twitch_channels: dict[str, dict[str, Emote]] = {}      # channel -> name -> Emote (Helix chat/emotes)
        self._twitch_ids: dict[str, dict[str, str]] = {}             # channel -> emote id -> name
        # Shared stores.
        self._global: dict[str, dict[str, Emote]] = {}   # provider -> token -> Emote (scope None)
        self._twitch_global: dict[str, Emote] = {}       # Helix chat/emotes/global
        # Active-channel views (the picker/completer and channel-less lookups).
        self._channel: dict[str, dict[str, Emote]] = {}  # provider -> token -> Emote
        self._twitch_channel: dict[str, Emote] = {}      # name -> Emote (subscriber sets)
        self._twitch_names: dict[str, str] = {}          # emote id -> name
        self._providers: dict[str, EmoteProvider] = {
            "7tv": SevenTvProvider(),
            "bttv": BttvProvider(),
            "ffz": FfzProvider(),
        }
        self._loaded_channel: str | None = None
        self._global_loaded = False  # global sets are channel-independent: fetch once

    # -- loading ---------------------------------------------------------------------

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        return channel.lstrip("#").lower()

    def _activate(self, channel: str) -> None:
        """Point the active-channel views (picker/completer and channel-less
        lookups) at `channel`'s per-channel maps."""
        self._loaded_channel = channel
        self._channels.setdefault(channel, {})
        self._twitch_channels.setdefault(channel, {})
        self._twitch_ids.setdefault(channel, {})
        self._channel = self._channels[channel]
        self._twitch_channel = self._twitch_channels[channel]
        self._twitch_names = self._twitch_ids[channel]

    def set_twitch_emotes(self, channel: str, id_to_name: dict[str, str]) -> None:
        """Twitch channel emotes for `channel` from Helix chat/emotes
        ({emote_id: name}); makes the channel active for the picker."""
        channel = self._normalize_channel(channel)
        self._twitch_channels[channel] = {
            name: Emote(
                id=eid,
                name=name,
                provider="twitch",
                urls=twitch_emote_urls(eid, self._theme),
                animated=False,
                scope=channel,
            )
            for eid, name in id_to_name.items()
            if name
        }
        self._twitch_ids[channel] = dict(id_to_name)
        self._activate(channel)

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
        """Fetch all enabled third-party providers for a channel (best-effort)
        into its per-channel store and make it the active channel. Global sets
        are fetched once on the first load and kept — they don't change per
        channel."""
        channel_name = self._normalize_channel(channel_name)
        tasks: list[tuple[str, str, object]] = []
        for name, provider in self._providers.items():
            if not self._settings.get(f"emote_providers.{name}", True):
                continue
            tasks.append((name, "channel", provider.fetch_for_channel(channel_name, twitch_id)))
            if not self._global_loaded:
                tasks.append((name, "global", provider.fetch_global()))
        results = await asyncio.gather(*[t for _, _, t in tasks], return_exceptions=True)
        scoped: dict[str, dict[str, Emote]] = {}
        for (name, kind, _), result in zip(tasks, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("%s %s emote fetch raised: %s", name, kind, result)
                continue
            target = scoped if kind == "channel" else self._global
            target[name] = {e.name: e for e in result}
        self._channels[channel_name] = scoped
        self._activate(channel_name)
        self._global_loaded = True
        counts = {name: len(m) for name, m in {**scoped, **self._global}.items()}
        log.info("Emotes for %s: %s", channel_name, counts)

    def clear_channel(self) -> None:
        """Full reset: drop every channel's scoped maps and the globals."""
        self._loaded_channel = None
        self._channels.clear()
        self._twitch_channels.clear()
        self._twitch_ids.clear()
        self._channel = {}
        self._twitch_channel = {}
        self._twitch_names = {}
        self._global.clear()
        self._twitch_global.clear()
        self._global_loaded = False

    def forget_channel(self, channel: str) -> None:
        """Drop one channel's scoped emotes (channel PART/tab close)."""
        channel = self._normalize_channel(channel)
        self._channels.pop(channel, None)
        self._twitch_channels.pop(channel, None)
        self._twitch_ids.pop(channel, None)
        if channel == self._loaded_channel:
            self._loaded_channel = None
            self._channel = {}
            self._twitch_channel = {}
            self._twitch_names = {}

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()

    # -- lookups ----------------------------------------------------------------------

    def enabled(self, provider: str) -> bool:
        return bool(self._settings.get(f"emote_providers.{provider}", provider == "twitch"))

    def _scoped_maps(
        self, channel: str | None
    ) -> tuple[dict[str, dict[str, Emote]], dict[str, Emote]]:
        """(third-party provider->token->Emote, twitch name->Emote) scoped maps
        for `channel` — the active channel's when `channel` is None."""
        if channel is None:
            return self._channel, self._twitch_channel
        channel = self._normalize_channel(channel)
        return self._channels.get(channel, {}), self._twitch_channels.get(channel, {})

    def lookup(self, token: str, channel: str | None = None) -> Emote | None:
        """First match in priority order (Twitch excluded — IRC emote tags are
        authoritative for Twitch emotes; third-party tokens only). A provider's
        channel map wins over its global map; `channel` picks which channel's
        scoped maps to prefer (active channel when None)."""
        scoped, _ = self._scoped_maps(channel)
        for provider in self._priority:
            if provider == "twitch" or not self.enabled(provider):
                continue
            emote = scoped.get(provider, {}).get(token)
            if emote is None:
                emote = self._global.get(provider, {}).get(token)
            if emote is not None:
                return emote
        return None

    def name_for_twitch_id(self, emote_id: str, channel: str | None = None) -> str | None:
        if channel is None:
            return self._twitch_names.get(emote_id)
        return self._twitch_ids.get(self._normalize_channel(channel), {}).get(emote_id)

    def lookup_by_name(self, token: str, channel: str | None = None) -> Emote | None:
        """Name lookup across every provider in priority order — for messages
        without IRC emote tags (self-sent optimistic rows, where Twitch's tag
        is never present). A provider's channel map wins over its global map;
        `channel` picks which channel's scoped maps to prefer."""
        scoped, twitch_scoped = self._scoped_maps(channel)
        for provider in self._priority:
            if not self.enabled(provider):
                continue
            if provider == "twitch":
                emote = twitch_scoped.get(token) or self._twitch_global.get(token)
            else:
                emote = scoped.get(provider, {}).get(token) or self._global.get(provider, {}).get(token)
            if emote is not None:
                return emote
        return None

    def split_emote_text(
        self, text: str, channel: str | None = None
    ) -> list[tuple[str, Emote | None]]:
        """Split a self-sent message into (text, emote) pieces the way
        Twitch's parser would, for optimistic rows (which carry no IRC emote
        tags). A token matches a known name whole (raw or trailing-
        punctuation-stripped, so `KEKW,` and `;P` work); otherwise the token
        is split on punctuation and each word-chunk matches a name whole —
        Twitch does NOT split adjacent emote names (`KappaKappa` stays text
        on twitch.tv), so the optimistic row must not either. `channel`
        restricts scoped matches to that channel's emotes.
        """
        pieces: list[tuple[str, Emote | None]] = []
        for token in re.split(r"(\s+)", text):
            if not token:
                continue
            if token.isspace():
                pieces.append((token, None))
                continue
            # Whole-token first: covers punctuation-named emotes (;P) and the
            # common `KEKW,` form in one piece.
            stripped = token.rstrip("".join(PUNCT_STRIP))
            if stripped != token:
                emote = self.lookup_by_name(stripped, channel)
                if emote is not None:
                    pieces.append((stripped, emote))
                    pieces.append((token[len(stripped):], None))
                    continue
            emote = self.lookup_by_name(token, channel)
            if emote is not None:
                pieces.append((token, emote))
                continue
            for seg in re.split(r"([A-Za-z0-9_]+)", token):
                if not seg:
                    continue
                if not _WORD_RE.fullmatch(seg):
                    pieces.append((seg, None))
                else:
                    emote = self.lookup_by_name(seg, channel)
                    pieces.append((seg, emote) if emote is not None else (seg, None))
        return pieces

    def space_twitch_emotes(self, text: str, channel: str | None = None) -> str:
        """Insert spaces between adjacent Twitch emote names in `text` so
        twitch.tv's whole-word parser renders them (`KappaKappa` shows as
        letters there otherwise). A word-chunk is respaced only when it
        decomposes entirely into two or more known Twitch names; mixed words
        like `KappaX` and third-party names (7TV/BTTV/FFZ — never rendered by
        twitch.tv) are left untouched, so the message only changes when the
        change makes twitch.tv render an emote. Used to normalize outbound
        Twitch sends; the optimistic row then splits the same text.
        """
        if not self.enabled("twitch"):
            return text
        _, twitch_scoped = self._scoped_maps(channel)
        names = set(self._twitch_global) | set(twitch_scoped)
        if not names:
            return text
        out: list[str] = []
        for token in re.split(r"(\s+)", text):
            if not token:
                continue
            if token.isspace():
                out.append(token)
                continue
            body, tail = token, ""
            stripped = token.rstrip("".join(PUNCT_STRIP))
            if stripped != token:
                body, tail = stripped, token[len(stripped):]
            parts: list[str] = []
            for seg in re.split(r"([A-Za-z0-9_]+)", body):
                if not seg:
                    continue
                if not _WORD_RE.fullmatch(seg):
                    parts.append(seg)
                    continue
                decomposed = self._decompose_twitch(seg, names)
                parts.append(" ".join(decomposed) if decomposed is not None else seg)
            out.append("".join(parts) + tail)
        return "".join(out)

    @staticmethod
    def _decompose_twitch(word: str, names: set[str]) -> list[str] | None:
        """Greedy longest-name decomposition of `word` into known names;
        None unless the WHOLE word decomposes into at least two names."""
        found: list[str] = []
        pos = 0
        while pos < len(word):
            matched = ""
            for end in range(len(word), pos, -1):
                if word[pos:end] in names:
                    matched = word[pos:end]
                    break
            if not matched:
                return None
            found.append(matched)
            pos += len(matched)
        return found if len(found) >= 2 else None

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
