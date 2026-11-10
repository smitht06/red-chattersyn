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
from .auth import DEFAULT_TWITCH_CLIENT_ID, TwitchAuth, TwitchUnreachable
from .helix import Helix
from .irc import MAX_CHANNELS, NotConnected, TwitchIrc
from .mapper import AuthFailed, TwitchMapper, twitch_emote_urls

log = logging.getLogger(__name__)

LAUNCH_CHECK_TIMEOUT = 10.0  # seconds start() waits for the token check before connecting
EXPIRED_MESSAGE = "Twitch sign-in expired — reconnect your account"
REJECTED_MESSAGE = "Twitch rejected the sign-in — reconnect your account"


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
            settings.get("twitch.client_id") or DEFAULT_TWITCH_CLIENT_ID,
            secrets,
            ports=tuple(settings.get("twitch.redirect_ports", [17821, 17822, 17823])),
        )
        self.helix: Helix | None = None
        self.irc: TwitchIrc | None = None
        self.tokens = None  # TwitchTokens once validated
        self._unreachable = False  # the last connect attempt / refresh could not reach Twitch
        # The saved login is in use but its token check hasn't completed yet.
        self._check_pending = False
        self._global_badges_loaded = False
        self._catch_up_task: asyncio.Task | None = None
        # One re-login at a time; set once a re-login's new token was handed
        # to a rebuilt connection, cleared by that connection's welcome.
        self._relogin_task: asyncio.Task | None = None
        self._relogin_spent = False
        # The fall-back a Helix call's definite rejection scheduled (once).
        self._expired_task: asyncio.Task | None = None
        # Read-only fall-backs run one at a time: two interleaved swaps
        # would leave an orphaned anonymous client running.
        self._fall_back_lock = asyncio.Lock()

    @property
    def unreachable(self) -> bool:
        """True from a connect attempt or token refresh that couldn't reach
        Twitch until chat next welcomes us."""
        return self._unreachable

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        if self.auth.configured() and self.auth.load_tokens() is not None:
            try:
                # Stop *waiting* at the limit: wait_for cancels ensure_valid's
                # waiting, while the shielded refresh inside TwitchAuth keeps
                # running and saves (its refresh token is single-use).
                self.tokens = await asyncio.wait_for(self.auth.ensure_valid(), LAUNCH_CHECK_TIMEOUT)
            except (TimeoutError, TwitchUnreachable) as exc:
                log.warning("Twitch token check unfinished (%r) — connecting with the saved login", exc)
                self._connect_unchecked()
            except Exception:
                log.exception("Twitch token check failed — connecting with the saved login")
                self._connect_unchecked()
            else:
                if self.tokens is None:
                    log.warning("stored Twitch login rejected — running read-only until re-auth")
                    await self._fall_back_read_only(EXPIRED_MESSAGE, delete=True)
                    return
                await self.ensure_helix()

        self.irc = self._make_irc()
        self.irc.start()
        self._join_desired()

    def _connect_unchecked(self) -> None:
        """The launch check couldn't finish: use the saved login as it is
        and check it on the next signed-in welcome."""
        self.tokens = self.auth.load_tokens()
        self._check_pending = self.tokens is not None
        self._set_unreachable()

    async def _fall_back_read_only(self, message: str, *, delete: bool) -> None:
        """Swap to an anonymous read-only connection and announce why.
        `delete` also removes the saved login (when Twitch rejected it)."""
        if delete:
            self.auth.clear()
        await self._cancel_tasks()  # supersedes an older fall-back too
        async with self._fall_back_lock:
            self.tokens = None
            self._check_pending = False
            self._relogin_spent = False
            if self.helix is not None:
                helix, self.helix = self.helix, None
                await helix.close()
            if self.irc is not None:
                irc, self.irc = self.irc, None
                await irc.stop()
            self.irc = self._make_irc()
            self.irc.start()
            self._join_desired()
            bridge.auth_state.emit("twitch", False, message)

    async def _cancel_tasks(self) -> None:
        """Stop every token-work task the service owns - catch-up, re-login,
        read-only fall-back - but never the calling one. Safe mid-refresh:
        the token refresh itself is shielded and finishes on its own, and
        TwitchAuth won't save it over a changed login."""
        current = asyncio.current_task()
        pending = []
        for name in ("_catch_up_task", "_relogin_task", "_expired_task"):
            task = getattr(self, name)
            if task is None or task is current:
                continue
            setattr(self, name, None)
            if not task.done():
                task.cancel()
                pending.append(task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _set_unreachable(self) -> None:
        self._unreachable = True
        self._emit_connection("unreachable", "")

    def _emit_connection(self, state: str, detail: str) -> None:
        bridge.connection_state.emit(
            {"platform": "twitch", "channel_id": "", "state": state, "detail": detail})

    def _on_irc_connected(self, signed_in: bool, source: TwitchIrc | None = None) -> None:
        self._unreachable = False
        if signed_in:
            self._relogin_spent = False
        self._emit_connection("connected", "signed_in" if signed_in else "anonymous")
        if signed_in and (self._catch_up_task is None or self._catch_up_task.done()):
            self._catch_up_task = asyncio.ensure_future(
                self._catch_up(source if source is not None else self.irc))

    async def _catch_up(self, source: TwitchIrc | None) -> None:
        """Run on each signed-in welcome of `source`: finish everything an
        outage or an offline launch left undone — the token check, the Helix
        client, the global badges/emotes and each channel's badges/emotes.
        Steps that already succeeded are not repeated. Stops as soon as
        `source` is no longer the live connection (sign-out, reconnect,
        read-only swap)."""
        try:
            if self._check_pending:
                try:
                    tokens = await self.auth.ensure_valid()
                except TwitchUnreachable:
                    return  # stay pending; the next welcome tries again
                if self.irc is not source:
                    return
                if tokens is None:
                    await self._fall_back_read_only(EXPIRED_MESSAGE, delete=True)
                    return
                self._check_pending = False
                self.tokens = tokens
            if self.helix is None:
                await self.ensure_helix()  # also loads the global badges/emotes
            elif not self._global_badges_loaded:
                await self._load_global_badges()
            if self.irc is not source:
                return
            for name in list(self._desired):
                self._kick_metadata(name)  # skips channels that already loaded
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Twitch catch-up failed")

    async def reconnect(self) -> bool:
        """Drop the IRC connection and start a fresh one — the recovery path
        after OAuth sign-in ("Connect Twitch"): the new token replaces the
        anonymous/old connection and every desired channel re-joins.
        False = the swap did not happen (the caller must not report the
        account as connected)."""
        await self._cancel_tasks()
        self._relogin_spent = False
        try:
            tokens = await self.auth.ensure_valid()
        except TwitchUnreachable as exc:
            log.warning("token refresh unreachable after sign-in (%r) — keeping the current connection", exc)
            tokens = None
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
        self._check_pending = False  # just checked
        if self.helix is None:
            await self.ensure_helix()
        else:
            self.helix.invalidate_token()
        if self.irc is not None:
            await self.irc.stop()
            self.irc = None
        self.irc = self._make_irc()
        self.irc.start()
        self._join_desired()
        return True

    async def disconnect(self) -> None:
        """Sign out: clear the stored tokens and swap the connection back to
        anonymous read-only (justinfan), re-joining every desired channel."""
        await self._cancel_tasks()
        self._relogin_spent = False
        self._check_pending = False
        self.auth.clear()
        self.tokens = None
        if self.helix is not None:
            await self.helix.close()
            self.helix = None
        if self.irc is not None:
            await self.irc.stop()
            self.irc = None
        self.irc = self._make_irc()
        self.irc.start()
        self._join_desired()

    async def _user_token(self) -> str | None:
        """Validated/refreshed access token for Helix's TokenProvider; None
        when re-authorization is needed. Syncs self.tokens so the IRC
        mapper's self_user_id follows a Helix-triggered refresh.

        TwitchUnreachable propagates: only that Helix action fails, the saved
        login and self.tokens stay as they are. A definite rejection also
        drops the signed-in connection (once, however many calls see it)."""
        tokens = await self.auth.ensure_valid()
        if tokens is None:
            if self.tokens is not None and (self._expired_task is None or self._expired_task.done()):
                log.warning("Twitch login rejected on an account action — running read-only until re-auth")
                self._expired_task = asyncio.ensure_future(
                    self._fall_back_read_only(EXPIRED_MESSAGE, delete=True))
            self.tokens = None
            return None
        self.tokens = tokens
        return tokens.access_token

    async def ensure_helix(self) -> None:
        """Create the Helix client when a user token exists (start() or a
        mid-session connect): start() skips it without tokens, and a restart
        should not be required for badges/metadata."""
        if self.helix is None and self.tokens is not None:
            self.helix = Helix(self.auth.client_id, self._user_token)
            await self._load_global_badges()

    async def rebuild_helix(self) -> None:
        """Recreate the Helix client after the settings client_id override
        changes; falls through to ensure_helix when none exists yet."""
        if self.helix is not None:
            await self.helix.close()
            self.helix = None
        await self.ensure_helix()

    def _make_irc(self) -> TwitchIrc:
        mapper = TwitchMapper(
            badge_lookup=self._badge_lookup,
            emote_name_lookup=self.registry.name_for_twitch_id,
            third_party_lookup=self.registry.lookup,
            emote_urls=functools.partial(twitch_emote_urls, theme=self._theme),
            self_user_id=(self.tokens.user_id if self.tokens else None),
            theme=self._theme,
        )
        token = self.tokens.access_token if self.tokens else None
        born: list[TwitchIrc] = []  # the client this callback belongs to
        irc = TwitchIrc(
            mapper,
            # Twitch requires the NICK to be the authenticated login, and
            # self_author() reports it — a justinfan nick on a token'd
            # connection would surface as "you".
            nick=(self.tokens.login if self.tokens else None),
            token=token,
            on_message=self._on_message,
            on_event=self._on_event,
            on_auth=lambda event: self._on_auth(event, born[0], token),
            on_connected=lambda: self._on_irc_connected(token is not None, born[0]),
            on_connect_failed=self._set_unreachable,
        )
        born.append(irc)
        return irc

    async def stop(self) -> None:
        await self._cancel_tasks()
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
        if not name:
            return True
        if name in self._desired:
            # Already joined (or deferred pre-start): still make sure the
            # metadata load wasn't skipped back then.
            self._kick_metadata(name)
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
        self._kick_metadata(name)
        return True

    def _join_desired(self) -> None:
        """Join every desired channel on a fresh connection. Channels that
        were requested before the service started (join_channel with no live
        IRC) are only in `_desired` — their metadata load was deferred, so
        kick it here or the channel keeps a bare join with no Twitch emotes."""
        for name in list(self._desired):
            self.irc.join(name)
            self._kick_metadata(name)

    def _kick_metadata(self, name: str) -> None:
        """Schedule badges/emotes loading for `name` when it hasn't run yet
        and the prerequisites (live IRC + Helix) exist. Anonymous runs have
        no Helix; their 7TV/BTTV/FFZ load is driven by _on_message room ids."""
        if self.irc is not None and self.helix is not None and name not in self._metadata_loaded:
            self._metadata_loaded.add(name)
            asyncio.ensure_future(self._load_channel_metadata(name))

    def part(self, name: str) -> None:
        name = name.lstrip("#").lower()
        self._desired.discard(name)
        self._metadata_loaded.discard(name)
        self.registry.forget_channel(name)
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
            return
        self._global_badges_loaded = True

    async def _load_channel_metadata(self, name: str) -> None:
        """Badges + Twitch emotes + third-party emotes for one channel. A Helix
        failure takes the channel out of `_metadata_loaded` so the next
        catch-up (chat welcome) redoes it; a channel Twitch reports as missing
        counts as loaded."""
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
            self.registry.set_twitch_emotes(name, id_to_name)
        except Exception as exc:
            log.warning("Helix metadata for %s failed: %s", name, exc)
            self._metadata_loaded.discard(name)
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
            # No user token / Helix client: the room-id IRC tag is the only
            # way to get the numeric id the third-party emote APIs need.
            room_id = msg.extra.get("room_id")
            if room_id:
                self._metadata_loaded.add(msg.channel_id)
                asyncio.ensure_future(self._load_anonymous_metadata(msg.channel_id, room_id))
        bridge.message.emit(msg)

    def _on_event(self, event) -> None:
        bridge.event.emit(event)

    def _on_auth(self, event: AuthFailed, source: TwitchIrc, token: str | None) -> None:
        """Twitch refused the login on `source`, a client built with `token`.
        Twitch then closes the socket and that client reconnects with the
        same token, so a refusal during a running re-login is just noise."""
        log.warning("Twitch auth notice: %s", event.reason)
        if source is not self.irc:
            return  # a replaced client's late refusal
        if token is None:
            return  # anonymous connections have no login to refresh
        if self._relogin_task is not None and not self._relogin_task.done():
            return  # single flight
        self._relogin_task = asyncio.ensure_future(self._relogin(source, token))

    async def _relogin(self, source: TwitchIrc, rejected_token: str) -> None:
        """Answer an IRC auth refusal: one refresh, one rebuilt connection. A
        second refusal before the rebuilt connection is welcomed means the new
        token is refused too — then go read-only and keep the login."""
        try:
            if self._relogin_spent:
                log.warning("Twitch refused the refreshed token — running read-only")
                await self._fall_back_read_only(REJECTED_MESSAGE, delete=False)
                return
            try:
                tokens = await self.auth.ensure_valid(rejected_token=rejected_token)
            except TwitchUnreachable as exc:
                log.warning("Twitch token refresh failed (%r) — will retry at the next refusal", exc)
                if self.irc is source:
                    self._set_unreachable()  # the client's backoff reconnect retries
                return
            if self.irc is not source:
                return  # reconnect / sign-out / read-only swapped the connection meanwhile
            if tokens is None:
                log.warning("Twitch login rejected on re-login — running read-only until re-auth")
                await self._fall_back_read_only(EXPIRED_MESSAGE, delete=True)
                return
            self.tokens = tokens
            self._check_pending = False
            if self.helix is not None:
                self.helix.invalidate_token()
            await self.irc.stop()
            self.irc = self._make_irc()
            self.irc.start()
            self._join_desired()
            self._relogin_spent = True
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Twitch re-login failed")
