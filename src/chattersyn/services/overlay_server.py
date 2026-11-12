"""The OBS browser source: a small local web server on 127.0.0.1.

GET /chat    the overlay page (static HTML)
GET /events  Server-Sent Events: `config` first, then the recent rows that
             haven't faded (oldest first), then live `message` / `remove` /
             `config` events. EventSource reconnects by itself and sends
             Last-Event-ID, so a reconnect gets no duplicates.

Hand-written HTTP like the OAuth loopback servers: no web framework. Each page
gets its own bounded queue; a page that stops reading is dropped instead of
holding up the others or the chat.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PAGE_PATH = Path(__file__).resolve().parent.parent / "assets" / "overlay" / "chat.html"
PING_SECONDS = 15.0
QUEUE_SIZE = 500
REQUEST_TIMEOUT = 10.0  # for the whole request line + headers
MAX_HEADERS = 100
REPLAY_REMOVES = 200
# DNS rebinding: a web page can't make the browser send its own name as Host.
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}

# The highest event id handed out in this process (see OverlayServer._next_id).
_last_event_id = 0


def load_page() -> bytes:
    """The overlay page shipped with the app."""
    return PAGE_PATH.read_bytes()


class _Client:
    def __init__(self, writer: asyncio.StreamWriter, queue_size: int):
        self.writer = writer
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_size)


class OverlayServer:
    def __init__(self, *, port: int, page: bytes, host: str = "127.0.0.1",
                 now: Callable[[], float] = time.time, queue_size: int = QUEUE_SIZE):
        self.host = host
        self._requested_port = port
        self._page = page
        self._now = now
        self._queue_size = queue_size
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[_Client] = set()
        self._config: dict[str, Any] = {}
        self._max_messages = 50
        self._fade_seconds = 30.0
        # Recent rows for replay: (event id, row id, event dict).
        self._recent: collections.deque[tuple[int, str, dict]] = collections.deque()
        # Recent removals, replayed too: a page that was away mustn't keep them.
        self._removes: collections.deque[tuple[int, list[str]]] = collections.deque(
            maxlen=REPLAY_REMOVES)
        # Event ids keep growing across restarts (milliseconds since 1970, and
        # past any id this process already used), so a page reconnecting with
        # an old Last-Event-ID still gets the new server's rows.
        self._next_id = max(int(time.time() * 1000), _last_event_id + 1)
        self._handlers: set[asyncio.Task] = set()

    # -- lifecycle ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self._requested_port

    @property
    def sockets(self) -> list:
        return list(self._server.sockets) if self._server is not None else []

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def start(self) -> bool:
        """Listen on host:port. False when the port can't be used."""
        if self._server is not None:
            return True
        try:
            self._server = await asyncio.start_server(
                self._handle, self.host, self._requested_port)
        except OSError as exc:
            log.warning("browser source: can't listen on %s:%s (%s)",
                        self.host, self._requested_port, exc)
            return False
        log.info("browser source on http://localhost:%s/chat", self.port)
        return True

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for client in list(self._clients):
            self._drop(client)
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)

    # -- publishing ---------------------------------------------------------------

    def set_config(self, config: dict[str, Any]) -> None:
        self._config = dict(config)
        self._broadcast(self._frame("config", self._config))

    def set_limits(self, *, max_messages: int, fade_seconds: float) -> None:
        self._max_messages = max(1, int(max_messages))
        self._fade_seconds = max(0.0, float(fade_seconds))
        self._trim()

    def publish_message(self, event: dict[str, Any]) -> None:
        event_id = self._take_id()
        self._recent.append((event_id, str(event.get("id", "")), event))
        self._trim()
        self._broadcast(self._frame("message", event, event_id))

    def publish_remove(self, ids: list[str]) -> None:
        if not ids:
            return
        gone = set(ids)
        self._recent = collections.deque(r for r in self._recent if r[1] not in gone)
        event_id = self._take_id()
        self._removes.append((event_id, list(ids)))
        self._broadcast(self._frame("remove", list(ids), event_id))

    # -- internals ------------------------------------------------------------------

    def _take_id(self) -> int:
        global _last_event_id
        event_id = self._next_id
        self._next_id += 1
        _last_event_id = max(_last_event_id, event_id)
        return event_id

    def _trim(self) -> None:
        while len(self._recent) > self._max_messages:
            self._recent.popleft()

    def _replay(self, after: int) -> list[bytes]:
        """Frames a (re)connecting page needs: recent rows not yet faded, and
        removals it may have missed, in event order."""
        cutoff = self._now() - self._fade_seconds if self._fade_seconds else None
        rows = [(eid, ev) for eid, _, ev in self._recent
                if eid > after and (cutoff is None or float(ev.get("arrived", 0)) >= cutoff)]
        frames = [(eid, self._frame("message", ev, eid)) for eid, ev in rows[-self._max_messages:]]
        if after:
            frames += [(eid, self._frame("remove", ids, eid))
                       for eid, ids in self._removes if eid > after]
        return [frame for _, frame in sorted(frames, key=lambda item: item[0])]

    @staticmethod
    def _frame(kind: str, data: Any, event_id: int | None = None) -> bytes:
        head = f"id: {event_id}\n" if event_id is not None else ""
        return f"{head}event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    def _broadcast(self, frame: bytes) -> None:
        for client in list(self._clients):
            try:
                client.queue.put_nowait(frame)
            except asyncio.QueueFull:
                log.info("browser source: dropping a page that stopped reading")
                self._drop(client)

    def _drop(self, client: _Client) -> None:
        self._clients.discard(client)
        with contextlib.suppress(asyncio.QueueFull):
            client.queue.put_nowait(None)  # tells its writer loop to stop
        # abort, not close: a stuck page's unsent data would keep close() waiting.
        with contextlib.suppress(Exception):
            client.writer.transport.abort()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            await self._serve(reader, writer)
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError,
                TimeoutError, ValueError, asyncio.LimitOverrunError):
            pass  # a client that went away, was too slow, or sent an oversized line
        except Exception:
            log.exception("browser source: request failed")
        finally:
            if task is not None:
                self._handlers.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[bytes, dict | None]:
        """Request line + headers; headers None when there are too many."""
        request = await reader.readline()
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                return request, headers
            if len(headers) >= MAX_HEADERS:
                return request, None
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request, headers = await asyncio.wait_for(self._read_request(reader), REQUEST_TIMEOUT)
        if headers is None:
            await self._reply(writer, "431 Request Header Fields Too Large", b"", "text/plain")
            return
        parts = request.decode("latin-1").split()
        method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
        path = path.split("?", 1)[0]
        if not _local_host(headers.get("host", "")):
            await self._reply(writer, "403 Forbidden", b"", "text/plain")
        elif method != "GET":
            await self._reply(writer, "405 Method Not Allowed", b"", "text/plain")
        elif path == "/chat":
            await self._reply(writer, "200 OK", self._page, "text/html; charset=utf-8")
        elif path == "/events":
            await self._stream(writer, headers.get("last-event-id", ""))
        else:
            await self._reply(writer, "404 Not Found", b"not found", "text/plain")

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: str, body: bytes, kind: str) -> None:
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n"
            f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def _stream(self, writer: asyncio.StreamWriter, last_event_id: str) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\n"
                     b"Cache-Control: no-store\r\nConnection: keep-alive\r\n\r\n")
        after = int(last_event_id) if last_event_id.isdigit() else 0
        writer.write(self._frame("config", self._config))
        for frame in self._replay(after):
            writer.write(frame)
        # Joins the live feed before the first await, so nothing published
        # between the replay and the live events can be missed.
        client = _Client(writer, self._queue_size)
        self._clients.add(client)
        try:
            await writer.drain()
            while True:
                try:
                    frame = await asyncio.wait_for(client.queue.get(), PING_SECONDS)
                except TimeoutError:
                    frame = b": ping\n\n"
                if frame is None:
                    return
                writer.write(frame)
                await writer.drain()
        finally:
            self._clients.discard(client)


def _local_host(host: str) -> bool:
    """A Host header naming this computer (any port), or none at all."""
    if not host:
        return True
    name = host.lower()
    name = name.split("]", 1)[0] + "]" if name.startswith("[") else name.rsplit(":", 1)[0]
    return name in ALLOWED_HOSTS
