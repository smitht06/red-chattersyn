"""Reply flow: banner state, mention insertion, Twitch-only reply fields."""

import asyncio
import datetime as dt

import pytest
from PySide6.QtWidgets import QPushButton

from chattersyn.config.settings import Settings
from chattersyn.models.message import Author, Message, MessageFlags, Platform
from chattersyn.services.send import SendCoordinator, SendTargets
from chattersyn.ui.input_widget import InputWidget
from chattersyn.ui.tab import Tab


def _msg(platform=Platform.TWITCH, login="alice", name="Alice") -> Message:
    return Message(
        id="m1", platform=platform, channel_id="forsen",
        ts=dt.datetime(2026, 10, 16, 9, 5, 7),
        author=Author(name=name, login=login), plain="hello",
    )


class _Twitch:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, channel, text, **kwargs):
        self.calls.append((channel, text, kwargs))
        return kwargs.get("optimistic")


class _YouTube:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, live_chat_id, text):
        self.calls.append((live_chat_id, text))
        return {"id": f"yt-{len(self.calls)}"}


# -- InputWidget ------------------------------------------------------------------------


def test_insert_mention_at_cursor(qapp):
    widget = InputWidget(Settings({}))
    widget.set_text("say ")
    widget.set_cursor_position(4)
    widget.insert_mention("bob")
    assert widget.text() == "say @bob "


def test_insert_mention_empty_input(qapp):
    widget = InputWidget(Settings({}))
    widget.insert_mention("bob")
    assert widget.text() == "@bob "


# -- Tab reply banner ---------------------------------------------------------------------


@pytest.fixture
def tab(qapp):
    return Tab(Settings({}), "forsen")


def test_show_reply_sets_banner_and_mention(tab):
    tab.show_reply("m1", "alice", "Alice")
    assert not tab._reply_banner.isHidden()
    assert "Alice" in tab._reply_label.text()
    assert tab.input.text() == "@Alice "
    assert tab._reply_parent == ("m1", "alice")


def test_consume_reply_prefix_intact(tab):
    tab.show_reply("m1", "alice", "Alice")
    consumed = tab.consume_reply_prefix("@Alice hello there")
    assert consumed == ("hello there", "m1", "alice")
    assert tab._reply_parent is None
    assert tab._reply_banner.isHidden()


def test_consume_reply_prefix_edited_away_keeps_state(tab):
    tab.show_reply("m1", "alice", "Alice")
    assert tab.consume_reply_prefix("plain message") is None
    # the banner stays: the user edited the mention, this is a plain send
    assert tab._reply_parent == ("m1", "alice")


def test_consume_without_banner_is_none(tab):
    assert tab.consume_reply_prefix("@Alice x") is None


def test_cancel_removes_prefix_and_emits(tab):
    tab.show_reply("m1", "alice", "Alice")
    got = []
    tab.reply_cancelled.connect(lambda: got.append(1))
    tab._reply_banner.findChild(QPushButton).click()
    assert got == [1]
    assert tab.input.text() == ""
    assert tab._reply_banner.isHidden()


def test_show_reply_replaces_previous_mention(tab):
    tab.show_reply("m1", "alice", "Alice")
    tab.input.set_text("@Alice hello")
    tab.show_reply("m2", "bob", "Bob")
    assert tab.input.text() == "@Bob hello"
    assert tab._reply_parent == ("m2", "bob")


def test_hide_reply_keeps_input_without_remove_flag(tab):
    tab.show_reply("m1", "alice", "Alice")
    tab.hide_reply()
    assert tab.input.text() == "@Alice "  # only state + banner cleared
    assert tab._reply_parent is None


def test_view_reply_signal_reaches_tab(tab, qapp):
    got = []
    tab.reply_requested.connect(got.append)
    msg = _msg()
    tab.view.reply_requested.emit(msg)
    assert got == [msg]


# -- SendCoordinator reply fields -----------------------------------------------------------


def test_make_optimistic_carries_reply_fields():
    coord = SendCoordinator(_Twitch(), _YouTube(), Settings({}))
    msg = coord._make_optimistic(
        Platform.TWITCH, SendTargets(twitch_channel="forsen"),
        "hi", False, False, "m1", "alice")
    assert msg.flags & MessageFlags.REPLY
    assert msg.reply_parent == "m1"
    assert msg.reply_parent_name == "alice"


def test_make_optimistic_without_reply_is_plain():
    coord = SendCoordinator(_Twitch(), _YouTube(), Settings({}))
    msg = coord._make_optimistic(
        Platform.TWITCH, SendTargets(twitch_channel="forsen"), "hi", False, False)
    assert msg.flags == MessageFlags.NONE
    assert msg.reply_parent is None
    assert msg.reply_parent_name is None


async def test_send_attaches_reply_to_twitch_only():
    twitch, youtube = _Twitch(), _YouTube()
    coord = SendCoordinator(twitch, youtube, Settings({}))
    coord.start()
    targets = SendTargets(twitch_channel="forsen", youtube_live_chat_id="chat1")
    optimistic = coord.send(
        "t1", "hello", targets, reply_parent_id="m1", reply_parent_login="alice")
    tw_msg, yt_msg = optimistic[Platform.TWITCH], optimistic[Platform.YOUTUBE]
    assert tw_msg.flags & MessageFlags.REPLY
    assert tw_msg.reply_parent == "m1"
    # YouTube has no reply protocol: plain message, no reply header
    assert not (yt_msg.flags & MessageFlags.REPLY)
    assert yt_msg.reply_parent is None
    await asyncio.sleep(0)  # let the workers execute
    assert twitch.calls[0][2]["reply_parent_id"] == "m1"
    assert twitch.calls[0][2]["reply_parent_login"] == "alice"
    assert youtube.calls[0] == ("chat1", "hello")
    await coord.stop()
