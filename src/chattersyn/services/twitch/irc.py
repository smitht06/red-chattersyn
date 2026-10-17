"""Twitch IRC over WebSocket: connect/caps/join pacing, reconnect with backoff,
rate-limited send with client-nonce echo dedupe."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import random
import time
import uuid

from websockets.asyncio.client import connect

from ...core.ratelimit import MinInterval, TokenBucket
from ...core.text import prepare_twitch_send
from ...models.message import Author, Message, MessageFlags, Platform, TextRun
from . import parse
from .mapper import (
    AuthFailed,
    ClearChat,
    ClearMsg,
    Names,
    Reconnect,
    RoomState,
    TwitchMapper,
    UserState,
)

log = logging.getLogger(__name__)

WS_URL = "wss://irc-ws.chat.twitch.tv:443"
SERVER = "irc-ws.chat.twitch.tv"
CAPS = "twitch.tv/tags twitch.tv/commands twitch.tv/membership"

JOIN_SPACING = 0.6  # seconds between JOINs (~16/10 s, Twitch allows 18)
MAX_CHANNELS = 90  # total joined channels cap
WELCOME_TIMEOUT = 15.0  # waiting for 001 before joins are released
STABLE_RESET_AFTER = 30.0  # connected this long -> backoff resets on drop
NONCE_TTL = 60.0  # pending echo nonce lifetime

BACKOFF_BASE = 1.0
BACKOFF_CAP = 15.0

SEND_CAPACITY = 20  # Twitch: 20 messages per 30 s
SEND_REFILL = SEND_CAPACITY / 30.0
SEND_SPACING = 1.1  # minimum seconds between sends


class NotConnected(Exception):
    pass


class NotAuthenticated(Exception):
    """Anonymous (justinfan) connections cannot send."""


class ForcedReconnect(Exception):
    """The server told us to reconnect (:tmi.twitch.tv RECONNECT)."""


class TwitchIrc:
    """One WebSocket connection to Twitch chat carrying any number of joined
    channels. Callbacks are plain callables invoked on the event loop."""

    def __init__(
        self,
        mapper: TwitchMapper,
        *,
        nick: str | None = None,
        token: str | None = None,
        on_message=None,
        on_event=None,  # ClearChat | ClearMsg | RoomState | Names
        on_auth=None,  # AuthFailed
        ws_url: str = WS_URL,
        now=None,
        backoff_base: float = BACKOFF_BASE,
        backoff_cap: float = BACKOFF_CAP,
        send_spacing: float = SEND_SPACING,
    ):
        self._mapper = mapper
        self._nick = nick or f"justinfan{random.randint(10000, 99999999)}"
        self._token = token
        self._on_message = on_message
        self._on_event = on_event
        self._on_auth = on_auth
        self._ws_url = ws_url
        self._now = now or time.monotonic
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

        self._ws = None
        self._stop_evt = asyncio.Event()
        self._run_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._welcome_time: float | None = None

        self._channels: set[str] = set()  # desired channels (lowercase, no #)
        self._join_queue: asyncio.Queue[str] = asyncio.Queue()
        self._join_spacing = MinInterval(JOIN_SPACING)

        self._bucket = TokenBucket(SEND_CAPACITY, SEND_REFILL)
        self._send_spacing = MinInterval(send_spacing)
        self._pending_nonces: dict[str, Message] = {}

        # Self identity from GLOBALUSERSTATE (for optimistic send rows).
        self.self_user_id: str | None = None
        self.display_name: str | None = None
        self.color: str | None = None
        self.badges: list = []

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._run_task is None or self._run_task.done():
            self._stop_evt.clear()
            self._run_task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._run_task is not None:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
            self._run_task = None

    @property
    def is_anonymous(self) -> bool:
        return not self._token

    def join(self, channel: str) -> bool:
        """Queue a JOIN for `channel` (with or without leading '#')."""
        channel = channel.lstrip("#").lower()
        if not channel:
            return True
        if channel in self._channels:
            return True
        if len(self._channels) >= MAX_CHANNELS:
            log.warning("join cap (%d) reached; not joining %s", MAX_CHANNELS, channel)
            return False
        self._channels.add(channel)
        self._join_queue.put_nowait(channel)
        return True

    def part(self, channel: str) -> None:
        channel = channel.lstrip("#").lower()
        self._channels.discard(channel)
        ws = self._ws
        if ws is not None:
            asyncio.ensure_future(self._send_raw(f"PART #{channel}"))

    async def _send_raw(self, line: str) -> None:
        try:
            if self._ws is not None:
                await self._ws.send(line)
        except Exception:
            log.debug("raw send failed (connection closing?)", exc_info=True)

    # -- connection loop ----------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while not self._stop_evt.is_set():
            try:
                await self._connect_and_read()
                if self._stop_evt.is_set():
                    break
                log.info("connection closed; reconnecting")
            except asyncio.CancelledError:
                raise
            except ForcedReconnect:
                log.info("server requested reconnect")
            except Exception:
                log.warning("IRC connection error", exc_info=True)

            # A connection that survived a while gets the short backoff.
            if self._welcome_time and self._now() - self._welcome_time > STABLE_RESET_AFTER:
                attempt = 0
            attempt += 1
            delay = min(self._backoff_cap, self._backoff_base * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
            log.info("reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=delay)
                break
            except TimeoutError:
                pass
        self._connected.clear()

    async def _connect_and_read(self) -> None:
        self._connected.clear()
        async with connect(self._ws_url) as ws:
            self._ws = ws
            await ws.send(f"CAP REQ :{CAPS}")
            if self._token:
                await ws.send(f"PASS oauth:{self._token}")
            await ws.send(f"NICK {self._nick}")
            pacer = asyncio.ensure_future(self._join_pacer())
            watchdog = asyncio.ensure_future(self._welcome_watchdog())
            try:
                # Read from the very first frame — the welcome is delivered by
                # this loop, so waiting for it *before* reading would deadlock.
                async for raw in ws:
                    for line in str(raw).splitlines():
                        if line:
                            self._handle_line(line)
            finally:
                pacer.cancel()
                watchdog.cancel()
                self._ws = None

    async def _welcome_watchdog(self) -> None:
        """Close the socket if 001 never arrives, so _run reconnects."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=WELCOME_TIMEOUT)
        except TimeoutError:
            log.warning("no 001 within %ss; dropping connection", WELCOME_TIMEOUT)
            ws = self._ws
            if ws is not None:
                await ws.close()

    async def _join_pacer(self) -> None:
        while not self._stop_evt.is_set():
            if not self._connected.is_set():
                await self._connected.wait()  # don't JOIN before the welcome
                continue
            channel = await self._join_queue.get()
            if channel not in self._channels:
                continue
            await self._join_spacing.wait()
            ws = self._ws
            if ws is None or not self._connected.is_set():
                self._join_queue.put_nowait(channel)  # requeue for next connection
                continue
            try:
                await ws.send(f"JOIN #{channel}")
            except Exception:
                self._join_queue.put_nowait(channel)

    def _requeue_joins(self) -> None:
        """After (re)connecting: drain stale entries and re-enqueue all desired channels."""
        while True:
            try:
                self._join_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        for channel in self._channels:
            self._join_queue.put_nowait(channel)

    # -- inbound ---------------------------------------------------------------

    def _handle_line(self, line: str) -> None:
        irc = parse.parse_irc_line(line)
        if irc is None:
            return
        cmd = irc.command
        if cmd == "PING":
            target = irc.params[0] if irc.params else SERVER
            asyncio.ensure_future(self._send_raw(f"PONG :{target}"))
            return
        if cmd == "001":
            self._connected.set()
            self._welcome_time = self._now()
            self._requeue_joins()
            return
        if cmd == "GLOBALUSERSTATE":
            self._capture_self(irc)
            return

        channel = self._channel_of(irc)
        try:
            result = self._mapper.map(irc, channel)
        except ForcedReconnect:
            raise
        except Exception:
            # A malformed line must not kill the connection.
            log.exception("error mapping IRC line: %r", irc.raw)
            return

        if isinstance(result, Message):
            self._handle_message(irc, result)
        elif isinstance(result, Reconnect):
            raise ForcedReconnect()
        elif isinstance(result, AuthFailed):
            if self._on_auth:
                self._on_auth(result)
        elif isinstance(result, (ClearChat, ClearMsg, RoomState, Names, UserState)) and self._on_event:
            self._on_event(result)

    @staticmethod
    def _channel_of(irc) -> str:
        # 353's params are [nick, =, #channel, ...names] — not channel-first.
        if irc.command == "353" and len(irc.params) >= 3:
            return irc.params[2].lstrip("#")
        if irc.params and irc.params[0].startswith("#"):
            return irc.params[0].lstrip("#")
        return ""

    def _capture_self(self, irc) -> None:
        tags = irc.tags
        if tags.get("user-id"):
            self.self_user_id = tags["user-id"]
        if tags.get("display-name"):
            self.display_name = tags["display-name"]
        if tags.get("color"):
            color = tags["color"]
            self.color = color if color.startswith("#") else f"#{color}"
        if tags.get("badges"):
            self.badges = self._mapper.parse_self_badges(tags)

    def _handle_message(self, irc, msg: Message) -> None:
        nonce = irc.tags.get("client-nonce", "")
        if nonce and nonce in self._pending_nonces:
            # Our own send echoed back: already shown optimistically; adopt the
            # server-assigned id so /reply and logging reference the real one.
            optimistic = self._pending_nonces.pop(nonce)
            if msg.id:
                optimistic.id = msg.id
            log.debug("echo deduped for nonce %s", nonce)
            return
        if self._on_message:
            self._on_message(msg)

    # -- outbound ----------------------------------------------------------------

    def self_author(self) -> Author | None:
        """The logged-in identity for optimistic messages (None when anonymous)."""
        if not self._token:
            return None
        return Author(
            name=self.display_name or self._nick,
            login=self._nick.lower(),
            user_id=self.self_user_id or "",
            color=self.color,
            badges=list(self.badges),
        )

    async def send(
        self,
        channel: str,
        text: str,
        *,
        reply_parent_id: str | None = None,
        reply_parent_login: str | None = None,
        optimistic: Message | None = None,
        action: bool | None = None,
    ) -> Message:
        """Rate-limited PRIVMSG; returns the optimistic Message (already shown
        by the caller — the echo is deduped). ``/me`` sends an ACTION.

        `optimistic`: when given, THAT Message object is registered for the
        echo (its id is adopted on echo) and returned — callers show it
        instantly instead of waiting for the rate bucket.
        `action`: override the /me detection — the coordinator already
        stripped the prefix, so re-parsing would lose it.
        """
        if not self._token:
            raise NotAuthenticated("anonymous (justinfan) connections cannot send")
        body, is_action = prepare_twitch_send(text)
        if action is not None:
            is_action = action
        payload = f"\x01ACTION {body}\x01" if is_action else body
        if not payload:
            raise ValueError("empty message")

        if self._ws is None:
            raise NotConnected()
        await self._bucket.acquire()
        await self._send_spacing.wait()
        ws = self._ws
        if ws is None:
            raise NotConnected()

        nonce = uuid.uuid4().hex
        tags = [f"client-nonce={nonce}"]
        if reply_parent_id:
            tags.append(f"reply-parent-msg-id={reply_parent_id}")
        if reply_parent_login:
            tags.append(f"reply-parent-user-login={reply_parent_login}")
        await ws.send(f"@{';'.join(tags)} PRIVMSG #{channel.lstrip('#')} :{payload}")

        msg = optimistic if optimistic is not None else Message(
            id=nonce,
            platform=Platform.TWITCH,
            channel_id=channel.lstrip("#"),
            ts=dt.datetime.now(),
            author=self.self_author() or Author(name=self._nick, login=self._nick.lower()),
            runs=[TextRun(body)],
            plain=body,
            flags=MessageFlags.ACTION if is_action else MessageFlags.NONE,
        )
        self._pending_nonces[nonce] = msg
        asyncio.get_running_loop().call_later(
            NONCE_TTL, lambda: self._pending_nonces.pop(nonce, None)
        )
        return msg
