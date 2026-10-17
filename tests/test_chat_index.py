"""ChatIndex: per-channel chatter snapshots for '@' autocomplete — fed by
message authors (both platforms) and the IRC Names (353) event."""

import pytest

from chattersyn.core.bus import bridge
from chattersyn.models.message import Author, Message, Platform
from chattersyn.services.chat_index import ChatIndex
from chattersyn.services.twitch.mapper import Names


@pytest.fixture
def index():
    idx = ChatIndex()
    idx.start()
    yield idx
    idx.stop()


def _msg(channel, name="", login=""):
    return Message.text(
        Platform.TWITCH, channel, Author(name=name, login=login), "hello")


def test_records_message_authors_per_channel(index):
    bridge.message.emit(_msg("#forsen", name="Forsen"))
    bridge.message.emit(_msg("#xqc", name="xQc"))
    assert index.users("#forsen") == ["Forsen"]
    assert index.users("#xqc") == ["xQc"]


def test_names_event_adds_chatters_first(index):
    bridge.event.emit(Names(channel="#forsen", names=["a", "B", "c"]))
    assert index.users("#forsen") == ["a", "B", "c"]


def test_case_insensitive_dedup_with_display_casing_upgrade(index):
    index.add_user("#forsen", "forsen")  # 353 lowercase snapshot
    index.add_user("#forsen", "Forsen")  # message author carries display casing
    assert index.users("#forsen") == ["Forsen"]


def test_author_login_falls_back_when_no_display_name(index):
    bridge.message.emit(_msg("#forsen", login="forsen"))
    assert index.users("#forsen") == ["forsen"]


def test_bound_drops_oldest():
    index = ChatIndex(max_users=3)
    index.start()
    for name in ("a", "b", "c", "d"):
        index.add_user("#x", name)
    assert index.users("#x") == ["b", "c", "d"]
    index.stop()


def test_stop_disconnects_from_bridge():
    idx = ChatIndex()
    idx.start()
    idx.stop()
    bridge.message.emit(_msg("#forsen", name="Forsen"))
    bridge.event.emit(Names(channel="#forsen", names=["a"]))
    assert idx.users("#forsen") == []


def test_start_is_idempotent():
    index = ChatIndex()
    index.start()
    index.start()  # a second start must not double-connect
    index.stop()   # one stop must sever both links
    bridge.message.emit(_msg("#forsen", name="Forsen"))
    assert index.users("#forsen") == []


def test_ignores_system_rows_and_blank_names(index):
    bridge.message.emit(Message.system(Platform.SYSTEM, "#forsen", "notice"))
    index.add_user("", "orphan")
    index.add_user("#forsen", "")
    index.add_user("#forsen", "   ")
    assert index.users("#forsen") == []


def test_unknown_channel_returns_empty(index):
    assert index.users("#nope") == []
