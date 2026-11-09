"""YouTubeService: the app-facing bundle of YouTube auth, resolution, reading
and sending. One watch task per target (video URL/id or channel handle).

State machine per watch:
    resolving -> live (read loop) -> ended/unavailable
              -> upcoming/offline (re-check on the schedule) -> live | stays offline
              -> not_found (channel targets keep re-checking, deep scan when found)
              -> auth_required (no OAuth token; keeps re-checking)

While a watch is not live, schedule.next_check_at decides when it checks next
(it depends on the kind of target, the stream's state, scheduled starts and
whether the tab is on screen — set_on_screen). The wait wakes early on a
visibility change, and re-reads the wall clock at least every 60 s because
asyncio.sleep runs on the monotonic clock, which stops during system suspend.

The gRPC streamList reader is primary when an API key or OAuth token exists
(API key = read-only public chats, OAuth = everything); the REST reader is
the fallback (OAuth only). Quota exhaustion is a terminal state for the day.

gRPC items carry only the author's channel id; YouTubeNames resolves ids to
display names via batched channels.list calls (1 unit per 50 ids, cached for
the session) before the first message from each chatter is emitted.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import grpc

from ...config.settings import Settings
from ...core.bus import bridge
from ...models.message import Author, Message, Platform
from .api import QUOTA, QuotaTracker, YouTubeApi, YoutubeApiError
from .auth import YouTubeAuth
from .mapper import map_item
from .names import YouTubeNames
from .quota import Pool
from .reader import GrpcReader, ReadQuotaExceeded, ReadRefused, RestReader, StreamEnded
from .resolver import LiveChatResolver, Resolution, ResolveState, _parse_dt, parse_target
from .schedule import WatchKind, next_check_at
from .sender import YoutubeSender, YoutubeSendError

log = logging.getLogger(__name__)

BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0
RE_RESOLVE_INTERVAL = 60.0  # retry after a failed check (error, auth required)
UPCOMING_RE_RESOLVE = 30.0  # live but no chat id yet
STREAM_STATS_INTERVAL = 120.0  # videos.list refresh while live (1 quota unit)
SLEEP_CHUNK = 60.0  # longest single sleep before the wall clock is read again
ON_SCREEN_RECHECK_AGE = dt.timedelta(seconds=60)  # coming on screen checks at once past this

_KINDS = {"video": WatchKind.VIDEO, "handle": WatchKind.CHANNEL,
          "channel": WatchKind.CHANNEL, "mine": WatchKind.MINE}


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _is_mine_target(target: str) -> bool:
    """True for MINE_TARGET — the signed-in user's own broadcast."""
    parsed = parse_target(target)
    return parsed is not None and parsed[0] == "mine"


def _watch_kind(target: str) -> WatchKind:
    """Unrecognised targets act as videos: their NOT_FOUND ends the watch."""
    parsed = parse_target(target)
    return _KINDS[parsed[0]] if parsed is not None else WatchKind.VIDEO


class WatchState(enum.Enum):
    RESOLVING = "resolving"
    LIVE = "live"
    UPCOMING = "upcoming"
    OFFLINE = "offline"
    ENDED = "ended"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"  # ended/disabled/forbidden
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_REQUIRED = "auth_required"


@dataclass
class YoutubeState:
    """Emitted on bridge.event; tabs key on watch_key and live_chat_id."""

    watch_key: str
    state: WatchState
    video_id: str | None = None
    live_chat_id: str | None = None
    channel_id: str | None = None  # the stream's channel (moderation owner check)
    title: str = ""
    channel_title: str = ""
    viewers: int = 0
    started_at: dt.datetime | None = None
    detail: str = ""


@dataclass
class _Watch:
    target: str
    watch_key: str
    stopped: bool = False
    task: asyncio.Task | None = None
    video_id: str | None = None
    live_chat_id: str | None = None
    channel_id: str | None = None  # the stream's channel (moderation owner check)
    read_page_token: str | None = None
    chat_ended: bool = False
    backoff: float = BACKOFF_BASE
    first_item_at: float | None = None  # for stable-connection backoff reset
    grpc_failures: int = 0  # consecutive; >=3 switches this watch to REST
    detail: str = ""
    title: str = ""
    channel_title: str = ""
    viewers: int = 0
    started_at: dt.datetime | None = None
    use_signin: bool = False  # key not allowed for this watch: read with the sign-in
    kind: WatchKind = field(init=False)  # follows target (see watch())
    on_screen: bool = True
    last_check: dt.datetime | None = None
    offline_since: dt.datetime = field(default_factory=_utc_now)  # watch start / live last seen
    deep: bool = True  # next resolve scans the channel's uploads in full
    check_now: bool = False  # came on screen with a stale last check
    next_check: dt.datetime | None = None  # the due time being waited for
    wake: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def __post_init__(self) -> None:
        self.kind = _watch_kind(self.target)


class YouTubeService:
    def __init__(
        self,
        settings: Settings,
        secrets,
        *,
        now: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self._settings = settings
        self._now = now  # aware UTC; injected by tests
        # The API key lives in the keyring; migrate a legacy plain-text
        # settings.json value on first sight so it never sits on disk again.
        self._api_key = secrets.get("youtube_api_key") or ""
        if not self._api_key:
            legacy = settings.get("youtube.api_key", "") or ""
            if legacy:
                secrets.set("youtube_api_key", legacy)
                settings.set("youtube.api_key", "")
                settings.save()
                self._api_key = legacy
        self.auth = YouTubeAuth(settings, secrets)
        self.quota = QuotaTracker(now=self._now)
        self.api = YouTubeApi(self.auth.access_token, self.quota,
                              get_key=lambda: self._api_key or None)
        self.resolver = LiveChatResolver(self.api)
        self.names = YouTubeNames(self.api)
        self.sender = YoutubeSender(
            self.api,
            block_urls=bool(settings.get("send.block_urls_for_youtube", True)),
        )
        self._grpc_channel = grpc.aio.secure_channel(
            "youtube.googleapis.com:443", grpc.ssl_channel_credentials())
        self._watches: dict[str, _Watch] = {}
        self.tokens = None  # Credentials once validated
        self._self_author: Author | None = None
        self._auth_connection = None

    # -- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        # Keep self.tokens in sync with the auth layer: a cleared/expired
        # grant (auth_state) must not leave stale Credentials behind.
        self._auth_connection = bridge.auth_state.connect(self._on_auth_state)
        if self.auth.configured():
            self.tokens = await self.auth.ensure_valid()
            if self.tokens is not None:
                bridge.auth_state.emit("youtube", True, "connected")
                # Awaited: by the time start() returns the connected identity
                # (and therefore YouTube moderation ownership) is known, so
                # MainWindow can run its owner check deterministically instead
                # of racing the first LIVE event.
                await self.load_identity()
        if self.tokens is None and not self._api_key:
            bridge.auth_state.emit(
                "youtube", False,
                "YouTube needs an API key or a connected Google account")

    def _on_auth_state(self, platform: str, ok: bool, detail: str) -> None:
        if platform != "youtube":
            return
        if not ok:
            self.tokens = None
            self._self_author = None
            return
        asyncio.ensure_future(self._resync_tokens())

    async def _resync_tokens(self) -> None:
        tokens = await self.auth.ensure_valid()
        if tokens is not None and tokens is not self.tokens:
            self.tokens = tokens
            await self.load_identity()

    def self_author(self) -> Author | None:
        """The connected account's identity (for optimistic messages)."""
        return self._self_author

    def self_channel_id(self) -> str:
        """The connected account's own channel id ("" when unknown)."""
        return self._self_author.user_id if self._self_author else ""

    def owns_channel(self, channel_id: str) -> bool:
        """True when the connected account owns `channel_id` — YouTube's
        moderation precondition (docs/m4.md: server ban + local delete)."""
        own = self.self_channel_id()
        return bool(channel_id and own and own.lower() == channel_id.lower())

    async def load_identity(self) -> None:
        """One channels.list mine=true (1 unit) for the own display name/avatar."""
        try:
            data = await self.api.channels_mine()
            item = (data.get("items") or [None])[0]
            if item is None:
                return
            snippet = item.get("snippet", {})
            thumb = ((snippet.get("thumbnails") or {}).get("default") or {})
            self._self_author = Author(
                name=snippet.get("title", ""),
                login=(item.get("id") or "").lower(),
                user_id=item.get("id", ""),
                avatar_url=thumb.get("url", ""),
            )
        except Exception as exc:
            log.warning("YouTube identity fetch failed: %s", exc)

    async def stop(self) -> None:
        if self._auth_connection is not None:
            bridge.auth_state.disconnect(self._auth_connection)
            self._auth_connection = None
        for watch in self._watches.values():
            watch.stopped = True
            if watch.task is not None:
                watch.task.cancel()
        for watch in self._watches.values():
            if watch.task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await watch.task
        self._watches.clear()
        await self.names.close()
        await self.api.close()
        await self.auth.close()
        await self._grpc_channel.close()

    # -- watching ---------------------------------------------------------------------

    def watch(self, target: str, watch_key: str) -> None:
        """Start resolving/reading `target` (video URL/id or channel handle)."""
        existing = self._watches.get(watch_key)
        if existing is not None:
            if target != existing.target:
                existing.target = target
                existing.kind = _watch_kind(target)
                existing.deep = True
            return
        watch = _Watch(target=target, watch_key=watch_key, offline_since=self._now())
        self._watches[watch_key] = watch
        watch.task = asyncio.ensure_future(self._watch_loop(watch_key, watch))

    def set_on_screen(self, watch_key: str, on_screen: bool) -> None:
        """Tell a watch whether its tab is on screen (selected, window not
        minimized). The next check moves to the last check plus the new
        interval; coming on screen checks at once when the last check is
        older than 60 s. Live chat reading is not affected."""
        watch = self._watches.get(watch_key)
        if watch is None:
            return
        coming_on = on_screen and not watch.on_screen
        watch.on_screen = on_screen
        if not on_screen:
            watch.check_now = False
        elif (coming_on and watch.last_check is not None
              and self._now() - watch.last_check > ON_SCREEN_RECHECK_AGE):
            watch.check_now = True
        watch.wake.set()

    def unwatch(self, watch_key: str) -> None:
        watch = self._watches.pop(watch_key, None)
        if watch is not None:
            watch.stopped = True
            if watch.task is not None:
                watch.task.cancel()

    # -- sending -----------------------------------------------------------------------

    async def send(self, live_chat_id: str, text: str) -> dict:
        """Forward to the sender; raises YoutubeSendError."""
        if not self.tokens:
            raise YoutubeSendError("auth_required", "no Google account connected")
        return await self.sender.send(live_chat_id, text)

    def can_read(self) -> bool:
        return bool(self.api._usable_key() or self.tokens)

    def set_api_key(self, value: str) -> None:
        """Live refresh after Settings accept (the key lives in the keyring)."""
        self._api_key = value.strip()
        self.api.reset_key_problem()

    def api_key_problem(self) -> str:
        """User-facing note when the API key is rejected, else ''."""
        if not self.api.key_problem:
            return ""
        if self.tokens:
            return "API key not working — using your Google sign-in"
        return "API key not working — YouTube reading paused"

    # -- watch loop ----------------------------------------------------------------------

    async def _watch_loop(self, watch_key: str, watch: _Watch) -> None:
        while not watch.stopped:
            try:
                if not await self._watch_iteration(watch_key, watch):
                    return  # terminal state — this watch is done
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let a watch task die silently
                log.warning("watch %s crashed: %s", watch.target, exc)
                watch.detail = str(exc)
                self._emit(watch_key, WatchState.RESOLVING, detail=str(exc))
                self._checked(watch)  # a failed check still counts: no retry at once
                await self._wait_seconds(watch, RE_RESOLVE_INTERVAL)

    async def _watch_iteration(self, watch_key: str, watch: _Watch) -> bool:
        """One resolve -> read cycle, then the wait for the next check.
        False = terminal, stop watching."""
        try:
            resolution = await self.resolver.resolve(watch.target, deep=watch.deep)
        except YoutubeApiError as exc:
            self._checked(watch)
            if exc.is_quota:
                self._emit(watch_key, WatchState.QUOTA_EXHAUSTED,
                           detail="YouTube quota exhausted (resets midnight PT)")
                self._system_row(watch, "YouTube quota exhausted — reading paused (resets midnight PT)")
                return False
            if exc.is_unauthenticated:
                # OAuth-only paths (mine=true, REST chat) with no/bad token:
                # surface a visible state instead of looping "resolving…".
                log.warning("resolve %s unauthenticated: %s", watch.target, exc)
                self._emit(watch_key, WatchState.AUTH_REQUIRED,
                           detail="connect a Google account to watch your stream")
                await self._wait_seconds(watch, RE_RESOLVE_INTERVAL)
                return True
            log.warning("resolve %s failed: %s", watch.target, exc)
            self._emit(watch_key, WatchState.RESOLVING, detail=str(exc))
            await self._wait_seconds(watch, RE_RESOLVE_INTERVAL)
            return True

        watch.deep = False
        self._checked(watch, saw_live=resolution.state is ResolveState.LIVE)
        watch.video_id = resolution.video_id
        watch.live_chat_id = resolution.live_chat_id
        watch.channel_id = resolution.channel_id
        watch.title = resolution.title
        watch.channel_title = resolution.channel_title
        watch.viewers = resolution.viewers
        watch.started_at = resolution.started_at
        self._emit_resolution(watch_key, resolution)

        if resolution.state is ResolveState.LIVE and not resolution.resolved:
            # Live but activeLiveChatId missing — it can lag a moment behind
            # the broadcast state; re-resolve instead of stopping the watch.
            await self._wait_seconds(watch, UPCOMING_RE_RESOLVE)
            return True

        state = resolution.state
        if resolution.resolved:
            outcome = await self._read_until_end(watch_key, watch)
            # The stream was live until now: the offline rhythm starts afresh.
            self._checked(watch, saw_live=True)
            if outcome == "ended":
                # Always emit ENDED (the tab must drop its stale live_chat_id
                # and moderation state); channel targets then keep watching.
                self._emit(watch_key, WatchState.ENDED, detail=watch.detail)
                state = ResolveState.ENDED
            else:  # "unavailable"
                self._emit(watch_key, WatchState.UNAVAILABLE, detail=watch.detail)
                state = ResolveState.OFFLINE  # maybe a new stream later
            if watch.kind is WatchKind.VIDEO:
                return False  # a specific video whose chat is over: stop watching
            # The channel's chat is over: reset and watch for a new stream.
            self._reset_watch(watch)
        elif state in (ResolveState.ENDED, ResolveState.NOT_FOUND):
            if watch.kind is WatchKind.VIDEO:
                return False  # a specific video that ended: stop watching
            if state is ResolveState.NOT_FOUND:
                # Found again later = a fresh start: look it up and scan deep.
                self.resolver.forget(watch.target)
                watch.deep = True

        starts = resolution.scheduled_starts
        await self._wait_until(watch, lambda: next_check_at(
            watch.kind, state,
            last_check=watch.last_check,
            offline_since=watch.offline_since,
            scheduled_starts=starts,
            on_screen=watch.on_screen,
        ))
        return True

    async def _read_until_end(self, watch_key: str, watch: _Watch) -> str:
        """Run the reader with reconnect/REST-fallback logic; a parallel
        stats task refreshes viewers/uptime every ~2 minutes (1 quota unit
        per call — gRPC chat reading itself is quota-free and unaffected).
        Returns "ended" | "unavailable"."""
        stop_event = asyncio.Event()
        stats_task = asyncio.ensure_future(self._stats_loop(watch_key, watch))
        try:
            return await self._read_loop(watch_key, watch, stop_event)
        finally:
            stats_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stats_task

    async def _stats_loop(self, watch_key: str, watch: _Watch) -> None:
        """Periodic videos.list refresh: pushes fresh LIVE emits so the tab's
        streamer bar tracks viewers/uptime while the chat is being read."""
        try:
            while not watch.stopped:
                await asyncio.sleep(STREAM_STATS_INTERVAL)
                if not watch.video_id:
                    continue
                try:
                    data = await self.api.videos(
                        watch.video_id,
                        signin=_is_mine_target(watch.target) or watch.use_signin)
                except YoutubeApiError as exc:
                    if exc.is_quota:
                        log.info("stats refresh stopped for %s: quota", watch.target)
                        return  # reading continues; stats are best-effort
                    log.warning("stats refresh failed for %s: %s", watch.target, exc)
                    continue
                if data is None or not data.get("items"):
                    continue
                details = data["items"][0].get("liveStreamingDetails", {}) or {}
                watch.viewers = int(details.get("concurrentViewers") or 0)
                watch.started_at = _parse_dt(details.get("actualStartTime"))
                self._emit(watch_key, WatchState.LIVE,
                           video_id=watch.video_id,
                           live_chat_id=watch.live_chat_id,
                           channel_id=watch.channel_id,
                           title=watch.title,
                           channel_title=watch.channel_title,
                           viewers=watch.viewers,
                           started_at=watch.started_at)
        except asyncio.CancelledError:
            raise

    async def _read_loop(self, watch_key: str, watch: _Watch,
                         stop_event: asyncio.Event) -> str:
        use_grpc = True  # gRPC primary; REST after repeated gRPC failures
        while not watch.stopped and not stop_event.is_set():
            if not self.can_read():
                self._emit(watch_key, WatchState.AUTH_REQUIRED,
                           detail="connect a Google account or set an API key to read chat")
                await self._sleep(watch, RE_RESOLVE_INTERVAL)
                continue
            self._emit(watch_key, WatchState.LIVE,
                       video_id=watch.video_id, live_chat_id=watch.live_chat_id,
                       channel_id=watch.channel_id,
                       title=watch.title, channel_title=watch.channel_title,
                       viewers=watch.viewers, started_at=watch.started_at)
            try:
                if use_grpc:
                    # streamList bills 5 units per (re)connection.
                    metadata, pool = await self._grpc_metadata(watch)
                    self.quota.charge(QUOTA["list"], pool)

                    async def get_metadata(metadata=metadata):
                        return metadata

                    reader = GrpcReader(self._grpc_channel, get_metadata)
                    token, offline = await reader.read(
                        watch.live_chat_id, watch.read_page_token,
                        self._on_item(watch, stop_event), stop_event=stop_event)
                    watch.read_page_token = token
                    watch.grpc_failures = 0
                    if offline:
                        self._system_row(watch, "Stream went offline")
                else:
                    reader = RestReader(self.api)
                    token = await reader.read(
                        watch.live_chat_id, watch.read_page_token,
                        self._on_item(watch, stop_event), stop_event=stop_event)
                    if token is not None:
                        watch.read_page_token = token  # resume mid-chat, not from the start
                    watch.grpc_failures = 0  # REST works — fresh gRPC chance next cycle
                if watch.chat_ended or watch.stopped:
                    return "ended"
                # Server EOF: reconnect from the saved token after a backoff.
                # A connection that stayed up 30s resets the backoff (stable).
                self._stable_reset(watch)
                await self._backoff_sleep(watch)
                continue
            except StreamEnded as exc:
                watch.detail = str(exc)
                return "unavailable"
            except ReadRefused as exc:
                if pool is not Pool.KEY:
                    # The sign-in itself was refused: retrying the same
                    # request would loop and burn shared quota.
                    watch.detail = str(exc)
                    return "unavailable"
                if exc.key_invalid:
                    self.api.key_problem = exc.reason
                    continue  # signed in: retry at once with Bearer; else the
                    # top of the loop reports AUTH_REQUIRED
                if self.tokens is not None:
                    watch.use_signin = True
                    continue
                watch.detail = str(exc)
                return "unavailable"
            except ReadQuotaExceeded as exc:
                self.quota.mark_exhausted(pool)
                watch.detail = f"quota exhausted: {exc}"
                return "unavailable"
            except YoutubeApiError as exc:
                if exc.is_quota:
                    watch.detail = "quota exhausted"
                    self._system_row(watch, watch.detail)
                    return "unavailable"
                if exc.is_chat_unavailable:
                    watch.detail = str(exc)
                    return "unavailable"
                log.warning("youtube rest read error: %s", exc)
                use_grpc = True  # REST failed; try gRPC next cycle
                await self._backoff_sleep(watch)
                continue
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                log.warning("youtube read transient error: %s", exc)
                if use_grpc:
                    watch.grpc_failures += 1
                    # REST live chat is OAuth-only, so API-key users keep
                    # retrying gRPC; OAuth users fall back after 3 failures.
                    if watch.grpc_failures >= 3 and self.tokens is not None:
                        log.warning("gRPC read failing repeatedly — switching to REST")
                        use_grpc = False
                await self._backoff_sleep(watch)
                continue
        return "ended"

    def _reset_watch(self, watch: _Watch) -> None:
        """Clear per-stream state so a channel target can re-resolve afresh."""
        watch.chat_ended = False
        watch.live_chat_id = None
        watch.channel_id = None
        watch.read_page_token = None
        watch.backoff = BACKOFF_BASE
        watch.grpc_failures = 0
        watch.first_item_at = None
        watch.detail = ""

    def _stable_reset(self, watch: _Watch) -> None:
        if watch.first_item_at is not None and time.monotonic() - watch.first_item_at >= 30:
            watch.backoff = BACKOFF_BASE
        watch.first_item_at = None

    async def _grpc_metadata(
        self, watch: _Watch
    ) -> tuple[tuple[tuple[str, str], ...], Pool]:
        """Credential for one stream connection and the pool it is billed to."""
        # Refresh in case the token expired mid-watch; fall back to the key.
        if self.auth.configured():
            self.tokens = await self.auth.ensure_valid()
        has_token = self.tokens is not None and bool(self.tokens.token)
        signin = _is_mine_target(watch.target) or watch.use_signin
        if has_token and (signin or not self.api._usable_key()):
            return (("authorization", f"Bearer {self.tokens.token}"),), Pool.SHARED
        if self._api_key and self.api._usable_key():
            return (("x-goog-api-key", self._api_key),), Pool.KEY
        return (), Pool.SHARED

    def _on_item(self, watch: _Watch, stop_event: asyncio.Event):
        def on_item(item: dict) -> None:
            if watch.stopped or stop_event.is_set():
                return
            if watch.first_item_at is None:
                watch.first_item_at = time.monotonic()
            msg = map_item(item)
            if watch.video_id:
                # stable identity for logging/search — live_chat_id is per-stream
                msg.extra.setdefault("video_id", watch.video_id)
            author = msg.author
            if author is not None and author.user_id and author.name == author.user_id:
                # gRPC items have no authorDetails: the mapper named this author
                # after the channel id. Swap in the display name when known;
                # otherwise hold the message briefly for the names resolver.
                name = self.names.cached(author.user_id)
                if name:
                    author.name = name
                else:
                    self.names.hold(msg)
                    return
            if msg.extra.get("youtube_type") == "chatEndedEvent":
                watch.chat_ended = True
                stop_event.set()
            bridge.message.emit(msg)

        return on_item

    # -- helpers ------------------------------------------------------------------------

    def _emit_resolution(self, watch_key: str, resolution: Resolution) -> None:
        state = {
            ResolveState.LIVE: WatchState.LIVE,
            ResolveState.UPCOMING: WatchState.UPCOMING,
            ResolveState.OFFLINE: WatchState.OFFLINE,
            ResolveState.ENDED: WatchState.ENDED,
            ResolveState.NOT_FOUND: WatchState.NOT_FOUND,
        }[resolution.state]
        self._emit(watch_key, state,
                   video_id=resolution.video_id,
                   live_chat_id=resolution.live_chat_id,
                   channel_id=resolution.channel_id,
                   title=resolution.title,
                   channel_title=resolution.channel_title,
                   viewers=resolution.viewers,
                   started_at=resolution.started_at)

    def _emit(self, watch_key: str, state: WatchState, **kwargs) -> None:
        bridge.event.emit(YoutubeState(watch_key=watch_key, state=state, **kwargs))

    def _system_row(self, watch: _Watch, text: str) -> None:
        bridge.message.emit(Message.system(
            Platform.YOUTUBE, watch.live_chat_id or watch.video_id or "",
            text, extra={"watch_key": watch.watch_key, "video_id": watch.video_id}))

    def _checked(self, watch: _Watch, *, saw_live: bool = False) -> None:
        """Record a check (and, if the stream was live, the time it was seen)."""
        now = self._now()
        watch.last_check = now
        watch.check_now = False  # this check is the one asked for
        if saw_live:
            watch.offline_since = now

    async def _wait_seconds(self, watch: _Watch, seconds: float) -> None:
        due = self._now() + dt.timedelta(seconds=seconds)
        await self._wait_until(watch, lambda: due)

    async def _wait_until(self, watch: _Watch, due: Callable[[], dt.datetime]) -> None:
        """Wait until `due()` against the wall clock, or until a check is
        asked for. A wake (e.g. a visibility change) recomputes `due()`."""
        while not watch.stopped:
            watch.wake.clear()
            if watch.check_now:
                return
            watch.next_check = due()
            left = (watch.next_check - self._now()).total_seconds()
            if left <= 0:
                return
            await self._nap(watch, min(left, SLEEP_CHUNK))

    async def _nap(self, watch: _Watch, seconds: float) -> None:
        """Sleep `seconds` or until the watch is woken."""
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(watch.wake.wait(), seconds)
        except asyncio.CancelledError:
            watch.stopped = True
            raise

    async def _sleep(self, watch: _Watch, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            watch.stopped = True
            raise

    async def _backoff_sleep(self, watch: _Watch) -> None:
        await self._sleep(watch, watch.backoff)
        watch.backoff = min(watch.backoff * 2, BACKOFF_CAP)
