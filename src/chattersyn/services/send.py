"""SendCoordinator: one Enter posts to both chats exactly once.

Responsibilities (plan M3):
- per-platform validation (Twitch 500 codepoints + IRC sanitizing;
  YouTube 200 chars + URL ban)
- dual-send truncate semantics: `truncate_mode` "highlight"/"silent"
  truncates to the stricter limit (200) for BOTH platforms with a
  "truncated" indicator; "block" refuses instead. Single-platform sends
  use that platform's own limit.
- validation failure on one platform offers "Send to Twitch only"
  (SendResult.offer) — never silently drop a platform
- per-platform bounded queues (20) with one worker each, preserving order
- YouTube rateLimitExceeded retries with backoff (3/6/12 s); Twitch never
  retries — Twitch silently drops instead of erroring, so the bucket is
  the only protection
- echo adoption: the YouTube insert-response id replaces the optimistic
  message id and is registered so the reader's echo can be deduped
  (note_echo); Twitch's echo dedupe stays inside IRC (client-nonce)

The coordinator is UI-agnostic: it emits SendResult on bridge.send_result;
MainWindow renders chips/system rows and shows the returned optimistic
Messages immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..core.bus import bridge
from ..core.text import (
    TWITCH_MESSAGE_LIMIT,
    YOUTUBE_MESSAGE_LIMIT,
    contains_url,
    prepare_twitch_send,
    truncate_codepoints,
)
from ..models.message import Author, Message, MessageFlags, Platform, TextRun
from .twitch.irc import NotAuthenticated, NotConnected
from .youtube.sender import YoutubeSendError

log = logging.getLogger(__name__)

QUEUE_SIZE = 20  # per-platform bounded backlog
RETRY_DELAYS = (3.0, 6.0, 12.0)  # YouTube rateLimitExceeded backoff
ECHO_TTL = 120.0  # server-id echo registry lifetime

# truncate_mode values
MODE_HIGHLIGHT = "highlight"  # truncate + "truncated" indicator (default)
MODE_SILENT = "silent"        # truncate, no indicator
MODE_BLOCK = "block"          # refuse over-limit messages


@dataclass
class SendTargets:
    """Where a send goes. At least one platform must be enabled."""

    twitch_channel: str = ""
    youtube_live_chat_id: str = ""
    twitch_enabled: bool = True
    youtube_enabled: bool = True

    @property
    def is_dual(self) -> bool:
        return self.twitch_enabled and self.youtube_enabled


@dataclass
class SendResult:
    """One platform's status transition; emitted on bridge.send_result."""

    key: str
    tab_key: str
    platform: Platform
    state: str  # queued|sending|sent|retrying|failed|validation_failed|cancelled
    text: str = ""
    truncated: bool = False
    error: str = ""
    retry_after: float = 0.0
    message_id: str = ""
    offer: str | None = None      # platform still sendable ("twitch")
    offer_label: str = ""         # e.g. "Send to Twitch only"


@dataclass
class _Job:
    key: str
    tab_key: str
    platform: Platform
    text: str               # final text that goes on the wire
    truncated: bool
    action: bool            # Twitch /me
    message: Message        # optimistic Message shown by the UI
    reply_parent_id: str | None = None
    reply_parent_login: str | None = None
    cancelled: bool = False


@dataclass
class _Request:
    key: str
    tab_key: str
    text: str
    targets: SendTargets
    reply_parent_id: str | None = None
    reply_parent_login: str | None = None


class SendCoordinator:
    def __init__(
        self,
        twitch_send: Callable[..., Awaitable[Message]],
        youtube_send: Callable[[str, str], Awaitable[dict]],
        settings,
        *,
        twitch_author: Callable[[], Author | None] | None = None,
        youtube_author: Callable[[], Author | None] | None = None,
    ) -> None:
        self._twitch_send = twitch_send
        self._youtube_send = youtube_send
        self._settings = settings
        self._twitch_author = twitch_author
        self._youtube_author = youtube_author
        self.mode = settings.get("send.truncate_mode", MODE_HIGHLIGHT)

        self._queues: dict[Platform, asyncio.Queue] = {
            Platform.TWITCH: asyncio.Queue(maxsize=QUEUE_SIZE),
            Platform.YOUTUBE: asyncio.Queue(maxsize=QUEUE_SIZE),
        }
        self._workers: dict[Platform, asyncio.Task] = {}
        self._inflight: dict[Platform, _Job | None] = {p: None for p in self._queues}
        self._requests: dict[str, _Request] = {}
        self._echo: dict[tuple[str, str], float] = {}  # (channel_id, id) -> expiry
        # (channel_id, plain) -> (optimistic Message, expiry): the reader can
        # echo our message before the insert response lands; matching on text
        # lets note_echo adopt the server id early (see note_echo).
        self._pending_echo: dict[tuple[str, str], tuple[Message, float]] = {}
        self._started = False

    # -- lifecycle --------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the per-platform workers (idempotent; needs a running loop)."""
        if self._started:
            return
        self._started = True
        for platform in self._queues:
            self._workers[platform] = asyncio.ensure_future(self._worker(platform))

    async def stop(self) -> None:
        for queue in self._queues.values():
            # A full queue can't take the sentinel — worker cancellation
            # below terminates it anyway.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    # -- sending ------------------------------------------------------------------------

    def send(
        self,
        tab_key: str,
        text: str,
        targets: SendTargets,
        *,
        reply_parent_id: str | None = None,
        reply_parent_login: str | None = None,
    ) -> dict[Platform, Message]:
        """Validate + enqueue for every enabled platform.

        Returns optimistic Messages keyed by platform — the UI shows them
        immediately. Partial validation failure emits validation_failed with
        an offer and sends nothing (the offer click calls send_offer).
        """
        if not text.strip() or not (targets.twitch_enabled or targets.youtube_enabled):
            return {}
        key = uuid.uuid4().hex
        request = _Request(
            key=key, tab_key=tab_key, text=text, targets=targets,
            reply_parent_id=reply_parent_id, reply_parent_login=reply_parent_login,
        )
        self._requests[key] = request

        prepared: dict[Platform, tuple[str, bool, bool]] = {}  # text, truncated, action
        failures: dict[Platform, str] = {}
        for platform in self._enabled_platforms(targets):
            error, out, truncated, action = self._validate(platform, text, targets.is_dual)
            if error:
                failures[platform] = error
            else:
                prepared[platform] = (out, truncated, action)

        if failures:
            offer = next(iter(prepared)) if len(prepared) == 1 else None
            for platform, error in failures.items():
                self._emit(SendResult(
                    key=key, tab_key=tab_key, platform=platform,
                    state="validation_failed", text=text, error=error,
                    offer=offer,
                    offer_label=f"Send to {self._platform_label(offer)} only" if offer else "",
                ))
            if offer is None:
                self._requests.pop(key, None)
            return {}

        optimistic: dict[Platform, Message] = {}
        for platform, (out, truncated, action) in prepared.items():
            # Replies are a Twitch-only protocol: the YouTube copy goes out
            # plain, with no inline reply header (Chatterino parity).
            reply_id = request.reply_parent_id if platform is Platform.TWITCH else None
            reply_login = request.reply_parent_login if platform is Platform.TWITCH else None
            msg = self._make_optimistic(
                platform, targets, out, truncated, action, reply_id, reply_login)
            job = _Job(
                key=key, tab_key=tab_key, platform=platform,
                text=out, truncated=truncated, action=action, message=msg,
                reply_parent_id=reply_id,
                reply_parent_login=reply_login,
            )
            try:
                self._queues[platform].put_nowait(job)
            except asyncio.QueueFull:
                self._emit(SendResult(
                    key=key, tab_key=tab_key, platform=platform,
                    state="failed", text=out, message_id=msg.id,
                    error=f"{self._platform_label(platform)} send queue is full — message dropped",
                ))
                continue
            optimistic[platform] = msg
            if platform is Platform.YOUTUBE:
                self._pending_echo[(msg.channel_id, msg.plain)] = (
                    msg, time.monotonic() + ECHO_TTL)
            self._emit(SendResult(
                key=key, tab_key=tab_key, platform=platform, state="queued",
                text=out, truncated=truncated, message_id=msg.id,
            ))
        # Nothing failed validation, so nothing can be offered — drop the request.
        self._requests.pop(key, None)
        return optimistic

    def send_offer(self, request_key: str, platform: Platform) -> Message | None:
        """Accept a validation-failure offer: send to the still-valid platform
        only, using that platform's own limit. Returns its optimistic Message."""
        request = self._requests.get(request_key)
        if request is None:
            return None
        targets = request.targets
        error, out, truncated, action = self._validate(platform, request.text, dual=False)
        if error:
            self._requests.pop(request_key, None)  # the offer is spent
            self._emit(SendResult(
                key=request_key, tab_key=request.tab_key, platform=platform,
                state="validation_failed", text=request.text, error=error,
            ))
            return None
        reply_id = request.reply_parent_id if platform is Platform.TWITCH else None
        reply_login = request.reply_parent_login if platform is Platform.TWITCH else None
        msg = self._make_optimistic(
            platform, targets, out, truncated, action, reply_id, reply_login)
        job = _Job(
            key=request_key, tab_key=request.tab_key, platform=platform,
            text=out, truncated=truncated, action=action, message=msg,
            reply_parent_id=reply_id,
            reply_parent_login=reply_login,
        )
        try:
            self._queues[platform].put_nowait(job)
        except asyncio.QueueFull:
            self._requests.pop(request_key, None)  # the offer is spent
            self._emit(SendResult(
                key=request_key, tab_key=request.tab_key, platform=platform,
                state="failed", text=out, message_id=msg.id,
                error=f"{self._platform_label(platform)} send queue is full — message dropped",
            ))
            return None
        self._requests.pop(request_key, None)
        self._emit(SendResult(
            key=request_key, tab_key=request.tab_key, platform=platform, state="queued",
            text=out, truncated=truncated, message_id=msg.id,
        ))
        return msg

    def discard_request(self, request_key: str) -> None:
        """Drop a pending offer without accepting it (banner hidden/closed)."""
        self._requests.pop(request_key, None)

    def cancel(self, request_key: str, platform: Platform) -> None:
        """Cancel a queued or in-flight send (the chips' [cancel] button)."""
        queue = self._queues[platform]
        for job in list(queue._queue):  # noqa: SLF001 — Queue has no public scan
            if getattr(job, "key", None) == request_key and job.platform is platform:
                job.cancelled = True
                self._emit(SendResult(
                    key=request_key, tab_key=job.tab_key, platform=platform,
                    state="cancelled", text=job.text, message_id=job.message.id,
                ))
                return
        inflight = self._inflight.get(platform)
        if inflight is not None and inflight.key == request_key:
            inflight.cancelled = True  # worker checks between retries

    def note_echo(self, msg: Message) -> bool:
        """True when `msg` is the server echo of something we just sent —
        the UI should drop it (the optimistic message is already shown)."""
        if msg.platform is not Platform.YOUTUBE:
            return False
        key = (msg.channel_id, msg.id)
        expires = self._echo.pop(key, None)
        if expires is None:
            # The reader may deliver the echo before the insert response
            # lands: match on text and adopt the server id onto the
            # optimistic message so the UI drops the duplicate.
            now = time.monotonic()
            for pending_key, (optimistic, deadline) in list(self._pending_echo.items()):
                if deadline < now:
                    self._pending_echo.pop(pending_key, None)  # lazy prune
                    continue
                if pending_key == (msg.channel_id, msg.plain):
                    self._pending_echo.pop(pending_key, None)
                    optimistic.id = msg.id
                    optimistic.extra.pop("pending", None)
                    return True
            return False
        if len(self._echo) > 100:  # lazy prune of stale registrations
            now = time.monotonic()
            for k, exp in list(self._echo.items()):
                if exp < now:
                    self._echo.pop(k, None)
        return expires > time.monotonic()

    # -- workers --------------------------------------------------------------------------

    async def _worker(self, platform: Platform) -> None:
        queue = self._queues[platform]
        while True:
            job = await queue.get()
            if job is None:
                queue.task_done()
                return
            self._inflight[platform] = job
            try:
                await self._execute(job)
            except Exception as exc:  # defensive: one bad job must not kill the worker
                log.exception("send worker %s crashed", platform)
                self._emit(SendResult(
                    key=job.key, tab_key=job.tab_key, platform=platform,
                    state="failed", text=job.text, message_id=job.message.id,
                    error=f"internal error: {exc}",
                ))
            finally:
                if self._inflight.get(platform) is job:
                    self._inflight[platform] = None
                queue.task_done()

    async def _execute(self, job: _Job) -> None:
        attempt = 0
        while True:
            if job.cancelled:
                self._emit(SendResult(
                    key=job.key, tab_key=job.tab_key, platform=job.platform,
                    state="cancelled", text=job.text, message_id=job.message.id,
                ))
                return
            self._emit(SendResult(
                key=job.key, tab_key=job.tab_key, platform=job.platform,
                state="sending" if attempt == 0 else "retrying",
                text=job.text, truncated=job.truncated, message_id=job.message.id,
                error="", retry_after=0.0,
            ))
            try:
                if job.platform is Platform.TWITCH:
                    await self._twitch_send(
                        job.message.channel_id, job.text,
                        optimistic=job.message,
                        action=job.action,
                        reply_parent_id=job.reply_parent_id,
                        reply_parent_login=job.reply_parent_login,
                    )
                    self._emit(SendResult(
                        key=job.key, tab_key=job.tab_key, platform=job.platform,
                        state="sent", text=job.text, truncated=job.truncated,
                        message_id=job.message.id,
                    ))
                else:
                    resource = await self._youtube_send(job.message.channel_id, job.text)
                    server_id = resource.get("id", "")
                    if server_id:
                        job.message.id = server_id
                        job.message.extra["pending"] = False
                        self._pending_echo.pop(
                            (job.message.channel_id, job.message.plain), None)
                        self._echo[(job.message.channel_id, server_id)] = (
                            time.monotonic() + ECHO_TTL)
                    self._emit(SendResult(
                        key=job.key, tab_key=job.tab_key, platform=job.platform,
                        state="sent", text=job.text, truncated=job.truncated,
                        message_id=server_id,
                    ))
                return
            except YoutubeSendError as exc:
                if exc.retryable and attempt < len(RETRY_DELAYS) and not job.cancelled:
                    delay = RETRY_DELAYS[attempt]
                    attempt += 1
                    self._emit(SendResult(
                        key=job.key, tab_key=job.tab_key, platform=job.platform,
                        state="retrying", text=job.text, truncated=job.truncated,
                        message_id=job.message.id, error=exc.detail, retry_after=delay,
                    ))
                    await asyncio.sleep(delay)
                    continue
                self._emit(SendResult(
                    key=job.key, tab_key=job.tab_key, platform=job.platform,
                    state="failed", text=job.text, message_id=job.message.id,
                    error=exc.detail,
                ))
                return
            except (NotConnected, NotAuthenticated) as exc:
                self._emit(SendResult(
                    key=job.key, tab_key=job.tab_key, platform=job.platform,
                    state="failed", text=job.text, message_id=job.message.id,
                    error=str(exc) or f"not connected to {self._platform_label(job.platform)}",
                ))
                return
            except Exception as exc:
                self._emit(SendResult(
                    key=job.key, tab_key=job.tab_key, platform=job.platform,
                    state="failed", text=job.text, message_id=job.message.id,
                    error=f"{self._platform_label(job.platform)} send failed: {exc}",
                ))
                return

    # -- validation --------------------------------------------------------------------------

    def _validate(
        self, platform: Platform, text: str, dual: bool
    ) -> tuple[str | None, str, bool, bool]:
        """-> (error | None, prepared_text, truncated, action)."""
        if platform is Platform.TWITCH:
            body, action = prepare_twitch_send(text)
            limit = YOUTUBE_MESSAGE_LIMIT if dual else TWITCH_MESSAGE_LIMIT
            return self._apply_limit(platform, body, limit, action)
        # YouTube: never gets the /me prefix (Twitch-only markup); the same
        # sanitizing (newline collapse) applies — the server rejects newlines.
        yt_text, _ = prepare_twitch_send(text)
        if contains_url(yt_text):
            return ("contains a URL — YouTube rejects links in live chat", yt_text, False, False)
        return self._apply_limit(platform, yt_text, YOUTUBE_MESSAGE_LIMIT, False)

    def _apply_limit(
        self, platform: Platform, text: str, limit: int, action: bool
    ) -> tuple[str | None, str, bool, bool]:
        if not text:
            return "empty message", text, False, action
        if len(text) <= limit:
            return None, text, False, action
        if self.mode == MODE_BLOCK:
            return (
                f"too long for {self._platform_label(platform)} ({len(text)}/{limit} characters)",
                text, False, action,
            )
        truncated = self.mode != MODE_SILENT
        return None, truncate_codepoints(text, limit), truncated, action

    # -- helpers ----------------------------------------------------------------------------

    def _make_optimistic(
        self, platform: Platform, targets: SendTargets,
        text: str, truncated: bool, action: bool,
        reply_parent_id: str | None = None,
        reply_parent_login: str | None = None,
    ) -> Message:
        channel_id = (
            targets.twitch_channel if platform is Platform.TWITCH
            else targets.youtube_live_chat_id
        )
        author = None
        if platform is Platform.TWITCH and self._twitch_author:
            author = self._twitch_author()
        elif platform is Platform.YOUTUBE and self._youtube_author:
            author = self._youtube_author()
        extra: dict = {"self": True}
        if truncated:
            extra["truncated"] = True
        if platform is Platform.YOUTUBE:
            extra["pending"] = True
        flags = MessageFlags.ACTION if action else MessageFlags.NONE
        if reply_parent_id:
            flags |= MessageFlags.REPLY
        return Message(
            id=f"optimistic-{uuid.uuid4().hex}",
            platform=platform,
            channel_id=channel_id,
            ts=dt.datetime.now(),
            author=author or Author(name="You"),
            runs=[TextRun(text)],
            plain=text,
            flags=flags,
            reply_parent=reply_parent_id or None,
            reply_parent_name=reply_parent_login or None,
            extra=extra,
        )

    @staticmethod
    def _enabled_platforms(targets: SendTargets) -> list[Platform]:
        platforms = []
        if targets.twitch_enabled and targets.twitch_channel:
            platforms.append(Platform.TWITCH)
        if targets.youtube_enabled and targets.youtube_live_chat_id:
            platforms.append(Platform.YOUTUBE)
        return platforms

    @staticmethod
    def _platform_label(platform: Platform | None) -> str:
        return {Platform.TWITCH: "Twitch", Platform.YOUTUBE: "YouTube"}.get(platform, "")

    @staticmethod
    def _emit(result: SendResult) -> None:
        bridge.send_result.emit(result)
