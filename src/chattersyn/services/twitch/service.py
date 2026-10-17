"""TwitchService: the app-facing bundle of Twitch auth, Helix metadata,
emote registry and the IRC connection. Events flow out through the Bridge."""

from __future__ import annotations

import asyncio
import functools
import logging

from ...config.settings import Settings
from ...core.bus import bridge
from ...models.message import Author, Message
from ...services.emotes.emote_registry import EmoteRegistry
from .auth import TwitchAuth
from .helix import Helix
from .irc import MAX_CHANNELS, NotConnected, TwitchIrc
from .mapper import AuthFailed, TwitchMapper, twitch_emote_urls

log = logging.getLogger(__name__)


class TwitchService:
    """Owns one IRC connection + metadata for all joined channels."""

    def __init__(self, settings: Settings, secrets) -> None:
        self._settings = settings
        self._secrets = secrets
        self._theme = settings.get("theme", "dark")
        self._badges: dict[tuple[str, str], object] = {}  # (set_id, version) -> Badge
        self._desired: set[str] = set()  # channels joined or waiting for connect
        self._metadata_loaded: set[str] = set()

        self.registry = EmoteRegistry(settings, self._theme)
        self.auth = TwitchAuth(
            settings.get("twitch.client_id", ""),
            self._secrets.get("twitch_client_secret") or "",
            secrets,
            ports=tuple(settings.get("twitch.redirect_ports", [17821, 17822, 17823])),
        )
        self.helix: Helix | None = None
        self.irc: TwitchIrc | None = None
        self.tokens = None  # TwitchTokens once validated

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        if self.auth.configured():
            self.helix = Helix(self.auth.client_id, self.auth.client_secret)
        if self.auth.configured():
            self.tokens = await self.auth.ensure_valid()
            if self.tokens is None:
                log.warning("stored Twitch tokens invalid — running read-only until re-auth")
                bridge.auth_state.emit("twitch", False, "stored tokens invalid — reconnect account")
        if self.helix is not None:
            await self._load_global_badges()

        self.irc = self._make_irc()
        self.irc.start()
        for name in list(self._desired):
            self.irc.join(name)

    async def reconnect(self) -> bool:
        """Drop the IRC connection and start a fresh one — the recovery path
        after OAuth sign-in ("Connect Twitch"): the new token replaces the
        anonymous/old connection and every desired channel re-joins.
        False = the swap did not happen (the caller must not report the
        account as connected)."""
        tokens = await self.auth.ensure_valid()
        if tokens is None:
            # A refresh failure right after sign-in must not silently swap
            # the connection for an anonymous justinfan one while the UI
            # reports the account as connected — keep what we have.
            log.warning("token refresh failed after sign-in — keeping the current connection")
            bridge.auth_state.emit(
                "twitch", False,
                "sign-in succeeded but the token refresh failed — try connecting again")
            return False
        self.tokens = tokens
        if self.irc is not None:
            await self.irc.stop()
            self.irc = None
        self.irc = self._make_irc()
        self.irc.start()
        for name in list(self._desired):
            self.irc.join(name)
        return True

    async def disconnect(self) -> None:
        """Sign out: clear the stored tokens and swap the connection back to
        anonymous read-only (justinfan), re-joining every desired channel."""
        self.auth.clear()
        self.tokens = None
        if self.irc is not None:
            await self.irc.stop()
            self.irc = None
        self.irc = self._make_irc()
        self.irc.start()
        for name in list(self._desired):
            self.irc.join(name)

    async def ensure_helix(self) -> None:
        """Create the Helix client after credentials arrive mid-session (the
        settings dialog): start() skips it when auth was unconfigured, and a
        restart should not be required for badges/metadata."""
        if self.helix is None and self.auth.configured():
            self.helix = Helix(self.auth.client_id, self.auth.client_secret)
            await self._load_global_badges()

    def _make_irc(self) -> TwitchIrc:
        mapper = TwitchMapper(
            badge_lookup=self._badge_lookup,
            emote_name_lookup=self.registry.name_for_twitch_id,
            third_party_lookup=self.registry.lookup,
            emote_urls=functools.partial(twitch_emote_urls, theme=self._theme),
            self_user_id=(self.tokens.user_id if self.tokens else None),
            theme=self._theme,
        )
        return TwitchIrc(
            mapper,
            token=self.tokens.access_token if self.tokens else None,
            on_message=self._on_message,
            on_event=self._on_event,
            on_auth=self._on_auth,
        )

    async def stop(self) -> None:
        if self.irc is not None:
            await self.irc.stop()
            self.irc = None
        await self.registry.close()
        await self.auth.close()
        if self.helix is not None:
            await self.helix.close()

    # -- channels --------------------------------------------------------------------

    async def join_channel(self, name: str) -> bool:
        """Join IRC + load badges/emotes metadata. False = join cap reached."""
        name = name.lstrip("#").lower()
        if not name or name in self._desired:
            return True
        if self.irc is None:
            # Service not started yet; will join on connect.
            if len(self._desired) >= MAX_CHANNELS:
                return False
            self._desired.add(name)
            return True
        if not self.irc.join(name):
            return False
        self._desired.add(name)
        if self.helix is not None and name not in self._metadata_loaded:
            self._metadata_loaded.add(name)
            asyncio.ensure_future(self._load_channel_metadata(name))
        return True

    def part(self, name: str) -> None:
        name = name.lstrip("#").lower()
        self._desired.discard(name)
        self._metadata_loaded.discard(name)
        if self.irc is not None:
            self.irc.part(name)

    async def send(self, channel: str, text: str, **kwargs) -> Message:
        """Forward to IRC; raises NotAuthenticated/NotConnected on failure."""
        if self.irc is None:
            raise NotConnected()
        return await self.irc.send(channel, text, **kwargs)

    def self_author(self) -> Author | None:
        """The logged-in identity for optimistic messages (None when anonymous)."""
        if self.irc is None:
            return None
        return self.irc.self_author()

    # -- metadata loading --------------------------------------------------------------

    async def _load_global_badges(self) -> None:
        try:
            global_badges = await self.helix.global_badges()
        except Exception as exc:
            log.warning("global badges failed: %s", exc)
            return
        for versions in global_badges.values():
            for version_id, badge in versions.items():
                self._badges[(badge.set_id, version_id)] = badge
        try:
            self.registry.set_twitch_global_emotes(await self.helix.global_emotes())
        except Exception as exc:
            log.warning("global Twitch emotes failed: %s", exc)

    async def _load_channel_metadata(self, name: str) -> None:
        """Badges + Twitch emotes + third-party emotes for one channel."""
        broadcaster_id: str | None = None
        try:
            user = await self.helix.user_by_login(name)
            if user is None:
                log.warning("no Twitch user %s (deleted/renamed?)", name)
                return
            broadcaster_id = user["id"]
            channel_badges = await self.helix.channel_badges(broadcaster_id)
            for versions in channel_badges.values():
                for version_id, badge in versions.items():
                    self._badges[(badge.set_id, version_id)] = badge
            id_to_name = await self.helix.channel_emotes(broadcaster_id)
            self.registry.set_twitch_emotes(id_to_name)
        except Exception as exc:
            log.warning("Helix metadata for %s failed: %s", name, exc)
        if broadcaster_id is None:
            return
        try:
            await self.registry.load_channel(name, broadcaster_id)
        except Exception as exc:  # best-effort providers already swallow errors
            log.warning("third-party emotes for %s failed: %s", name, exc)

    async def _load_anonymous_metadata(self, name: str, room_id: str) -> None:
        """7TV/BTTV/FFZ for anonymous runs (Twitch names/badges need Helix)."""
        try:
            await self.registry.load_channel(name, room_id)
        except Exception as exc:  # providers are best-effort; never fatal
            log.warning("anonymous emote load for %s failed: %s", name, exc)

    def _badge_lookup(self, set_id: str, version_id: str):
        return self._badges.get((set_id, version_id))

    # -- event routing -------------------------------------------------------------------

    def _on_message(self, msg: Message) -> None:
        if self.helix is None and msg.channel_id not in self._metadata_loaded:
            # No app credentials: the room-id IRC tag is the only way to get
            # the numeric id the third-party emote APIs need.
            room_id = msg.extra.get("room_id")
            if room_id:
                self._metadata_loaded.add(msg.channel_id)
                asyncio.ensure_future(self._load_anonymous_metadata(msg.channel_id, room_id))
        bridge.message.emit(msg)

    def _on_event(self, event) -> None:
        bridge.event.emit(event)

    def _on_auth(self, event: AuthFailed) -> None:
        log.warning("Twitch auth notice: %s", event.reason)
        bridge.auth_state.emit("twitch", False, event.reason)
