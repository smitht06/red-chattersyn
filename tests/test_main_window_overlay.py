"""OBS-1/2/4/5/7/8/9/12/13/14/16/22/25: the Stream tab's chat reaches the browser source."""

import asyncio
import contextlib
import json
import socket

import pytest
from PySide6.QtCore import QSettings

from chattersyn.config.settings import Settings
from chattersyn.models.message import Author, Message, MessageFlags, Platform
from chattersyn.services.twitch.mapper import ClearChat, ClearMsg
from chattersyn.ui.main_window import MainWindow

CHANNEL = "redcyberdragon29"


@pytest.fixture
def qsettings(tmp_path):
    return QSettings(str(tmp_path / "qt.ini"), QSettings.Format.IniFormat)


def _settings(**browser_source):
    conf = {"enabled": True, "port": 0}
    conf.update(browser_source)
    return Settings({"browser_source": conf})


async def _window(qsettings, settings):
    w = MainWindow(settings, demo=True, qsettings=qsettings)
    for feed in list(w._feeds):
        feed.stop()
    w._feeds.clear()
    await w._apply_overlay()
    tab = w._streamer_tabs[0]
    tab.twitch_channel = CHANNEL
    return w, tab


async def _close(w):
    await w._stop_overlay()
    w.close()


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
    @classmethod
    async def open(cls, port):
        self = cls()
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port, limit=2 ** 20)
        self.writer.write(b"GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await self.writer.drain()
        _OPEN.append(self.writer)
        await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), 5)
        return self

    async def next(self, timeout=5):
        fields = {}
        while True:
            raw = await asyncio.wait_for(self.reader.readline(), timeout)
            if raw == b"":
                raise ConnectionError("closed")
            line = raw.decode().rstrip("\n")
            if not line:
                if fields:
                    return fields["event"], json.loads(fields["data"])
                continue
            if not line.startswith(":"):
                key, _, value = line.partition(":")
                fields[key] = value.lstrip(" ")

    async def next_of(self, kind, timeout=5):
        while True:
            event, data = await self.next(timeout)
            if event == kind:
                return data

    async def nothing_of(self, kind, wait=0.4):
        try:
            while True:
                event, _ = await self.next(wait)
                if event == kind:
                    return False
        except TimeoutError:
            return True

    def close(self):
        self.writer.close()


def _chat(text="hello", login="bob", msg_id="m1", **kw):
    return Message.text(Platform.TWITCH, CHANNEL, Author(name=login.title(), login=login),
                        text, id=msg_id, **kw)


async def test_off_by_default(qapp, qsettings):
    """OBS-1."""
    w = MainWindow(Settings({}), demo=True, qsettings=qsettings)
    try:
        await w._apply_overlay()
        assert w._overlay is None
        assert w._overlay_status == "Off."
    finally:
        await _close(w)


async def test_row_reaches_the_page(qapp, qsettings):
    """OBS-2, OBS-7, OBS-25: a row in the (demo) Stream tab appears on the page."""
    w, tab = await _window(qsettings, _settings())
    try:
        assert w._overlay_status == "Running — add it in OBS as a Browser Source."
        page = await _Stream.open(w._overlay.port)
        await page.next_of("config")
        tab.add_message(_chat("hello"))
        tab._flush()
        data = await page.next_of("message")
        assert data["name"] == "Bob" and data["parts"] == [{"t": "text", "v": "hello"}]
        page.close()
    finally:
        await _close(w)


async def test_own_message_reaches_the_page(qapp, qsettings):
    """OBS-8: optimistic rows never pass the bridge, but they're in the tab."""
    w, tab = await _window(qsettings, _settings())
    try:
        page = await _Stream.open(w._overlay.port)
        await page.next_of("config")
        own = _chat("my message", login="me", msg_id="optimistic-1", extra={"self": True})
        tab.add_message(own)
        tab._flush()
        assert (await page.next_of("message"))["id"] == "optimistic-1"
        page.close()
    finally:
        await _close(w)


async def test_events_shown_notes_hidden(qapp, qsettings):
    """OBS-9."""
    w, tab = await _window(qsettings, _settings())
    try:
        page = await _Stream.open(w._overlay.port)
        await page.next_of("config")
        tab.add_message(Message.system(Platform.TWITCH, CHANNEL, "Twitch connected"))
        tab.add_message(_chat("Bob subscribed!", msg_id="s1",
                              flags=MessageFlags.SUBSCRIPTION,
                              extra={"msg_id": "sub", "usernotice": True}))
        tab._flush()
        data = await page.next_of("message")
        assert data["id"] == "s1" and data["kind"] == "sub"
        assert await page.nothing_of("message")
        page.close()
    finally:
        await _close(w)


async def test_deleted_message_removed(qapp, qsettings):
    """OBS-13."""
    w, tab = await _window(qsettings, _settings())
    try:
        page = await _Stream.open(w._overlay.port)
        await page.next_of("config")
        tab.add_message(_chat(msg_id="m1"))
        tab._flush()
        await page.next_of("message")
        w._on_bridge_event(ClearMsg(channel=CHANNEL, target_msg_id="m1", login="bob"))
        assert await page.next_of("remove") == ["m1"]
        page.close()
    finally:
        await _close(w)


async def test_timeout_removes_all_rows_and_unban_does_not_restore(qapp, qsettings):
    """OBS-14, OBS-22."""
    w, tab = await _window(qsettings, _settings())
    try:
        page = await _Stream.open(w._overlay.port)
        await page.next_of("config")
        tab.add_message(_chat("one", msg_id="a1"))
        tab.add_message(_chat("two", msg_id="a2"))
        tab.add_message(_chat("other", login="carol", msg_id="c1"))
        tab._flush()
        for _ in range(3):
            await page.next_of("message")
        w._on_bridge_event(ClearChat(channel=CHANNEL, user="bob", duration=None))
        removed = set()
        while removed != {"a1", "a2"}:
            removed |= set(await page.next_of("remove"))
        w._on_bridge_event(ClearChat(channel=CHANNEL, user="bob", duration=0))  # unban
        assert await page.nothing_of("message")
        page.close()
    finally:
        await _close(w)


async def test_row_deleted_before_flush_never_shown(qapp, qsettings):
    """Review Focus 2."""
    w, tab = await _window(qsettings, _settings())
    try:
        page = await _Stream.open(w._overlay.port)
        await page.next_of("config")
        tab.add_message(_chat(msg_id="gone"))
        tab.view.message_model.mark_deleted("gone")  # still pending: no signal
        tab._flush()
        assert await page.nothing_of("message")
        page.close()
    finally:
        await _close(w)


async def test_removed_after_id_change(qapp, qsettings):
    """Own messages get Twitch's real id later (in place): a delete by the new
    id still removes the row the page knows by the old one."""
    w, tab = await _window(qsettings, _settings())
    try:
        page = await _Stream.open(w._overlay.port)
        await page.next_of("config")
        own = _chat("mine", login="me", msg_id="optimistic-9")
        tab.add_message(own)
        tab._flush()
        await page.next_of("message")
        own.id = "real-9"
        w._on_bridge_event(ClearMsg(channel=CHANNEL, target_msg_id="real-9", login="me"))
        assert await page.next_of("remove") == ["optimistic-9"]
        page.close()
    finally:
        await _close(w)


async def test_restyle_reaches_open_pages(qapp, qsettings):
    """OBS-16."""
    w, tab = await _window(qsettings, _settings())
    try:
        page = await _Stream.open(w._overlay.port)
        await page.next_of("config")
        conf = dict(w._settings.get("browser_source"))
        conf["font_size"] = 28
        w._settings.set("browser_source", conf)
        await w._apply_overlay()
        assert (await page.next_of("config"))["font_size"] == 28
        page.close()
    finally:
        await _close(w)


async def test_turning_off_stops_listening(qapp, qsettings):
    w, tab = await _window(qsettings, _settings())
    try:
        port = w._overlay.port
        conf = dict(w._settings.get("browser_source"))
        conf["enabled"] = False
        w._settings.set("browser_source", conf)
        await w._apply_overlay()
        assert w._overlay is None and w._overlay_status == "Off."
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
    finally:
        await _close(w)


async def test_port_change_moves_the_page(qapp, qsettings):
    """OBS-5."""
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    new_port = free.getsockname()[1]
    free.close()
    w, tab = await _window(qsettings, _settings())
    try:
        old_port = w._overlay.port
        conf = dict(w._settings.get("browser_source"))
        conf["port"] = new_port
        w._settings.set("browser_source", conf)
        await w._apply_overlay()
        assert w._overlay.port == new_port
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", old_port)
    finally:
        await _close(w)


async def test_port_in_use_status(qapp, qsettings):
    """OBS-4."""
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    try:
        w = MainWindow(_settings(port=port), demo=True, qsettings=qsettings)
        try:
            await w._apply_overlay()
            assert w._overlay is None
            assert w._overlay_status == f"Port {port} is in use — choose another."
        finally:
            await _close(w)
    finally:
        blocker.close()


async def test_bad_hand_edited_port_reports_instead_of_crashing(qapp, qsettings):
    """Final review #14."""
    w = MainWindow(_settings(port="not a port"), demo=True, qsettings=qsettings)
    try:
        await w._apply_overlay()
        assert w._overlay is None
        assert w._overlay_status == "Port not a port is in use — choose another." or \
            w._overlay_status.startswith("Port ")
    finally:
        await _close(w)
