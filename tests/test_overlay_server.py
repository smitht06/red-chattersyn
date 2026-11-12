"""OBS-2/6/7/10/11/13: the local overlay server — page, live events, replay."""

import asyncio
import contextlib
import json
import socket

import pytest

from chattersyn.services.overlay_server import OverlayServer

PAGE = b"<!doctype html><title>chat</title>"


def _event(i, text="hi", arrived=1000.0):
    return {"id": f"m{i}", "platform": "twitch", "name": "Bob", "color": None,
            "badges": [], "parts": [{"t": "text", "v": text}], "ts": "12:00",
            "arrived": arrived, "kind": "normal", "bar": None}


async def _server(**kw):
    kw.setdefault("now", lambda: 1000.0)
    server = OverlayServer(port=0, page=PAGE, **kw)
    assert await server.start()
    return server


async def _get(port, path, method="GET"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    _OPEN.append(writer)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return data.decode("utf-8", "replace")


_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close_streams():
    """Close every test connection, even when a test fails midway (an unclosed
    StreamWriter raises a ResourceWarning later, in some other test)."""
    yield
    while _OPEN:
        writer = _OPEN.pop()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


class _Stream:
    """A minimal EventSource: opens /events and parses events."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    @classmethod
    async def open(cls, port, last_event_id=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=2 ** 20)
        extra = f"Last-Event-ID: {last_event_id}\r\n" if last_event_id else ""
        writer.write(f"GET /events HTTP/1.1\r\nHost: localhost\r\n{extra}\r\n".encode())
        await writer.drain()
        _OPEN.append(writer)
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        stream = cls(reader, writer)
        stream.head = head.decode()
        return stream

    async def next(self):
        fields = {}
        while True:
            raw = await asyncio.wait_for(self.reader.readline(), 5)
            if raw == b"":
                raise ConnectionError("stream closed")
            line = raw.decode().rstrip("\n")
            if line == "":
                if fields:
                    return fields
                continue
            if line.startswith(":"):
                continue
            key, _, value = line.partition(":")
            fields[key] = value.lstrip(" ")

    async def next_of(self, kind):
        while True:
            event = await self.next()
            if event.get("event") == kind:
                return event

    def close(self):
        self.writer.close()


async def test_chat_page_served():
    """OBS-2."""
    server = await _server()
    try:
        reply = await _get(server.port, "/chat")
    finally:
        await server.stop()
    assert reply.startswith("HTTP/1.1 200")
    assert "text/html" in reply and reply.endswith(PAGE.decode())


async def test_unknown_path_and_method():
    server = await _server()
    try:
        assert (await _get(server.port, "/nope")).startswith("HTTP/1.1 404")
        assert (await _get(server.port, "/chat", "POST")).startswith("HTTP/1.1 405")
    finally:
        await server.stop()


async def test_events_start_with_config_then_replay():
    server = await _server()
    server.set_config({"font_size": 20})
    server.set_limits(max_messages=50, fade_seconds=30)
    server.publish_message(_event(1))
    server.publish_message(_event(2))
    try:
        stream = await _Stream.open(server.port)
        assert "text/event-stream" in stream.head
        first = await stream.next()
        assert first["event"] == "config" and json.loads(first["data"]) == {"font_size": 20}
        replay = [json.loads((await stream.next_of("message"))["data"])["id"] for _ in range(2)]
        assert replay == ["m1", "m2"]  # oldest first
        stream.close()
    finally:
        await server.stop()


async def test_replay_skips_faded_and_limits():
    clock = {"now": 1000.0}
    server = await _server(now=lambda: clock["now"])
    server.set_limits(max_messages=2, fade_seconds=30)
    server.publish_message(_event(1, arrived=900.0))  # older than 30 s: faded
    server.publish_message(_event(2, arrived=990.0))
    server.publish_message(_event(3, arrived=995.0))
    server.publish_message(_event(4, arrived=999.0))
    try:
        stream = await _Stream.open(server.port)
        await stream.next_of("config")
        ids = [json.loads((await stream.next_of("message"))["data"])["id"] for _ in range(2)]
        assert ids == ["m3", "m4"]
        stream.close()
    finally:
        await server.stop()


async def test_live_message_reaches_two_pages():
    """OBS-7, OBS-11."""
    server = await _server()
    try:
        a = await _Stream.open(server.port)
        b = await _Stream.open(server.port)
        await a.next_of("config")
        await b.next_of("config")
        server.publish_message(_event(7, "hello"))
        for stream in (a, b):
            data = json.loads((await stream.next_of("message"))["data"])
            assert data["parts"] == [{"t": "text", "v": "hello"}]
            stream.close()
    finally:
        await server.stop()


async def test_remove_event_and_buffer():
    """OBS-13: removed rows leave open pages and aren't replayed."""
    server = await _server()
    server.set_limits(max_messages=50, fade_seconds=0)
    server.publish_message(_event(1))
    try:
        live = await _Stream.open(server.port)
        await live.next_of("message")
        server.publish_remove(["m1"])
        removed = await live.next_of("remove")
        assert json.loads(removed["data"]) == ["m1"]
        live.close()
        again = await _Stream.open(server.port)
        await again.next_of("config")
        server.publish_message(_event(2))
        assert json.loads((await again.next_of("message"))["data"])["id"] == "m2"
        again.close()
    finally:
        await server.stop()


async def test_reconnect_with_last_event_id_has_no_duplicates():
    """OBS-10 / Review Focus 3."""
    server = await _server()
    server.set_limits(max_messages=50, fade_seconds=0)
    try:
        first = await _Stream.open(server.port)
        await first.next_of("config")
        server.publish_message(_event(1))
        seen = await first.next_of("message")
        first.close()
        server.publish_message(_event(2))
        back = await _Stream.open(server.port, last_event_id=seen["id"])
        await back.next_of("config")
        assert json.loads((await back.next_of("message"))["data"])["id"] == "m2"
        back.close()
    finally:
        await server.stop()


async def test_config_pushed_live():
    """OBS-16 (server side): a restyle reaches open pages."""
    server = await _server()
    try:
        stream = await _Stream.open(server.port)
        await stream.next_of("config")
        server.set_config({"font_size": 28})
        assert json.loads((await stream.next_of("config"))["data"]) == {"font_size": 28}
        stream.close()
    finally:
        await server.stop()


async def test_port_in_use():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        server = OverlayServer(port=blocker.getsockname()[1], page=PAGE)
        assert await server.start() is False
        assert not server.running
    finally:
        blocker.close()


async def test_binds_localhost_only():
    """OBS-6."""
    server = await _server()
    try:
        assert server.host == "127.0.0.1"
        assert all(sock.getsockname()[0] == "127.0.0.1" for sock in server.sockets)
    finally:
        await server.stop()


async def test_stuck_client_does_not_block_others():
    """Review Focus 1: a page that stops reading is dropped; others keep going."""
    server = await _server(queue_size=20)
    server.set_limits(max_messages=5, fade_seconds=0)
    try:
        stuck = await _Stream.open(server.port)  # never reads again
        live = await _Stream.open(server.port)
        await live.next_of("config")
        seen = []

        async def read_live():
            while True:
                event = await live.next_of("message")
                seen.append(json.loads(event["data"])["id"])
                if seen[-1] == "m299":
                    return
        reader = asyncio.ensure_future(read_live())
        big = "x" * 100_000
        for i in range(300):  # ~30 MB: far more than the stuck page's socket buffers
            server.publish_message(_event(i, big))
            await asyncio.sleep(0.002)
        await asyncio.wait_for(reader, 30)
        assert seen[-1] == "m299" and len(seen) == 300
        assert server.client_count == 1
        stuck.close()
        live.close()
    finally:
        await server.stop()


async def test_stop_closes_pages():
    server = await _server()
    stream = await _Stream.open(server.port)
    await stream.next_of("config")
    await server.stop()
    with pytest.raises((asyncio.IncompleteReadError, ConnectionError, TimeoutError)):
        while True:
            line = await asyncio.wait_for(stream.reader.readline(), 2)
            if line == b"":
                raise ConnectionError("closed")
    assert not server.running


# -- final-review hardening ----------------------------------------------------------------


async def _raw(port, request: bytes):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    _OPEN.append(writer)
    writer.write(request)
    await writer.drain()
    return (await asyncio.wait_for(reader.read(), 5)).decode("latin-1")


async def test_foreign_host_refused():
    """Final review #6 (DNS rebinding): only localhost names are served."""
    server = await _server()
    try:
        bad = await _raw(server.port, b"GET /chat HTTP/1.1\r\nHost: evil.example:17830\r\n\r\n")
        assert bad.startswith("HTTP/1.1 403")
        for host in (b"localhost:17830", b"127.0.0.1", b"[::1]:17830"):
            ok = await _raw(server.port, b"GET /chat HTTP/1.1\r\nHost: " + host + b"\r\n"
                            b"Connection: close\r\n\r\n")
            assert ok.startswith("HTTP/1.1 200"), host
    finally:
        await server.stop()


async def test_too_many_headers_refused():
    """Final review #5."""
    server = await _server()
    try:
        headers = b"".join(b"X-%d: y\r\n" % i for i in range(150))
        reply = await _raw(server.port, b"GET /chat HTTP/1.1\r\nHost: localhost\r\n" + headers + b"\r\n")
        assert reply.startswith("HTTP/1.1 431")
    finally:
        await server.stop()


async def test_event_ids_keep_growing_across_servers():
    """Final review #9: a page reconnecting to a restarted server with its old
    Last-Event-ID must still get the new server's rows."""
    first = await _server()
    first.publish_message(_event(1))
    last_seen = first._recent[-1][0]  # the id a page got for that row
    await first.stop()
    second = await _server()
    try:
        assert second._next_id > last_seen
    finally:
        await second.stop()


async def test_removes_are_replayed_after_reconnect():
    """Final review #10."""
    server = await _server()
    server.set_limits(max_messages=50, fade_seconds=0)
    try:
        page = await _Stream.open(server.port)
        await page.next_of("config")
        server.publish_message(_event(1))
        seen = await page.next_of("message")
        page.close()
        server.publish_remove(["m1"])  # while the page is away
        back = await _Stream.open(server.port, last_event_id=seen["id"])
        await back.next_of("config")
        assert json.loads((await back.next_of("remove"))["data"]) == ["m1"]
    finally:
        await server.stop()
