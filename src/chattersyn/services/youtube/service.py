"""YouTubeService: the app-facing bundle of YouTube auth, resolution, reading
and sending. One watch task per target (video URL/id or channel handle).

State machine per watch:
    resolving -> live (read loop) -> ended/unavailable
              -> upcoming/offline (poll re-resolve) -> live | stays offline
              -> not_found (channel targets keep re-checking)

The gRPC streamList reader is primary when an API key or OAuth token exists
(API key = read-only public chats, OAuth = everything); the REST reader is
the fallback (OAuth only). Quota exhaustion is a terminal state for the day.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import enum
import logging
import time
from dataclasses import dataclass

import grpc

from ...config.settings import Settings
from ...core.bus import bridge
from ...models.message import Author, Message, Platform
from .api import QUOTA, QuotaTracker, YouTubeApi, YoutubeApiError
from .auth import YouTubeAuth
from .mapper import map_item
from .reader import GrpcReader, RestReader, StreamEnded
from .resolver import LiveChatResolver, Resolution, ResolveState, _parse_dt
from .sender import YoutubeSender, YoutubeSendError

log = logging.getLogger(__name__)

BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0
RE_RESOLVE_INTERVAL = 60.0  # offline/upcoming re-check
UPCOMING_RE_RESOLVE = 30.0
STREAM_STATS_INTERVAL = 120.0  # videos.list refresh while live (1 quota unit)


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


class YouTubeService:
    def __init__(self, settings: Settings, secrets) -> None:
        self._settings = settings
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
        self.quota = QuotaTracker()
        self.api = YouTubeApi(self.auth.access_token, self.quota)
        self.resolver = LiveChatResolver(self.api)
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
        await self.api.close()
        await self.auth.close()
        await self._grpc_channel.close()

    # -- watching ---------------------------------------------------------------------

    def watch(self, target: str, watch_key: str) -> None:
        """Start resolving/reading `target` (video URL/id or channel handle)."""
        existing = self._watches.get(watch_key)
        if existing is not None:
            existing.target = target
            return
        watch = _Watch(target=target, watch_key=watch_key)
        self._watches[watch_key] = watch
        watch.task = asyncio.ensure_future(self._watch_loop(watch_key, watch))

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
        return bool(self._api_key or self.tokens)

    def set_api_key(self, value: str) -> None:
        """Live refresh after Settings accept (the key lives in the keyring)."""
        self._api_key = value.strip()

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
                await self._sleep(watch, RE_RESOLVE_INTERVAL)

    async def _watch_iteration(self, watch_key: str, watch: _Watch) -> bool:
        """One resolve -> read cycle. False = terminal, stop watching."""
        try:
            resolution = await self.resolver.resolve(watch.target)
        except YoutubeApiError as exc:
            if exc.is_quota:
                self._emit(watch_key, WatchState.QUOTA_EXHAUSTED,
                           detail="YouTube quota exhausted (resets midnight PT)")
                self._system_row(watch, "YouTube quota exhausted — reading paused (resets midnight PT)")
                return False
            log.warning("resolve %s failed: %s", watch.target, exc)
            self._emit(watch_key, WatchState.RESOLVING, detail=str(exc))
            await self._sleep(watch, RE_RESOLVE_INTERVAL)
            return True

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
            await self._sleep(watch, UPCOMING_RE_RESOLVE)
            return True

        if resolution.resolved:
            outcome = await self._read_until_end(watch_key, watch)
            if outcome == "ended":
                # Always emit ENDED (the tab must drop its stale live_chat_id
                # and moderation state); channel targets then keep watching.
                self._emit(watch_key, WatchState.ENDED, detail=watch.detail)
                if self._is_channel_target(watch.target):
                    # The channel's chat ended: reset and watch for a new stream.
                    self._reset_watch(watch)
                    await self._sleep(watch, RE_RESOLVE_INTERVAL)
                    return True
                return False  # a specific video whose chat ended: stop watching
            if outcome == "unavailable":
                self._emit(watch_key, WatchState.UNAVAILABLE, detail=watch.detail)
                if self._is_channel_target(watch.target):
                    self._reset_watch(watch)
                    await self._sleep(watch, RE_RESOLVE_INTERVAL)  # maybe a new stream
                    return True
                return False

        if resolution.state is ResolveState.UPCOMING:
            await self._sleep(watch, UPCOMING_RE_RESOLVE)
        elif resolution.state is ResolveState.OFFLINE:
            await self._sleep(watch, RE_RESOLVE_INTERVAL)
        else:  # ENDED / NOT_FOUND
            if not self._is_channel_target(watch.target):
                return False  # a specific video that ended: stop watching
            await self._sleep(watch, RE_RESOLVE_INTERVAL)
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
                    data = await self.api.videos(watch.video_id)
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
                    self.quota.charge(QUOTA["list"])
                    reader = GrpcReader(self._grpc_channel, self._grpc_metadata)
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

    async def _grpc_metadata(self) -> tuple[tuple[str, str], ...]:
        # Refresh in case the token expired mid-watch; fall back to the key.
        if self.auth.configured():
            self.tokens = await self.auth.ensure_valid()
        if self.tokens is not None and self.tokens.token:
            return (("authorization", f"Bearer {self.tokens.token}"),)
        if self._api_key:
            return (("x-goog-api-key", self._api_key),)
        return ()

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
            if msg.extra.get("youtube_type") == "chatEndedEvent":
                watch.chat_ended = True
                stop_event.set()
            bridge.message.emit(msg)

        return on_item

    # -- helpers ------------------------------------------------------------------------

    @staticmethod
    def _is_channel_target(target: str) -> bool:
        from .resolver import parse_target

        parsed = parse_target(target)
        return parsed is not None and parsed[0] in ("handle", "channel")

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

    async def _sleep(self, watch: _Watch, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            watch.stopped = True
            raise

    async def _backoff_sleep(self, watch: _Watch) -> None:
        await self._sleep(watch, watch.backoff)
        watch.backoff = min(watch.backoff * 2, BACKOFF_CAP)
