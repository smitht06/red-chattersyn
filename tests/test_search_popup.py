"""Search popup: snapshot isolation, results model, keys, go-to navigation."""

import datetime as dt

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from chattersyn.config.settings import Settings
from chattersyn.models.message import Author, Message, Platform
from chattersyn.ui.chat_view import ChatView
from chattersyn.ui.message_model import MessageModel
from chattersyn.ui.search_popup import SearchPopup


def _msg(text, msg_id, name="Alice", login="alice", platform=Platform.TWITCH) -> Message:
    return Message(
        id=msg_id, platform=platform, channel_id="forsen",
        ts=dt.datetime(2026, 10, 16, 9, 5, 7),
        author=Author(name=name, login=login), plain=text,
    )


@pytest.fixture
def entries(qapp):  # qapp: every popup test constructs widgets
    return [
        ("tag-a", _msg("hello world", "m1")),
        ("tag-a", _msg("kappa kappa", "m2", name="Bob", login="bob")),
        ("tag-b", _msg("https://x.com link", "m3")),
    ]


# -- results model -------------------------------------------------------------------


def test_results_model_roles(entries):
    popup = SearchPopup(Settings({}), entries)
    model = popup._results.model()
    assert model.rowCount() == 0  # no initial text
    model.set_results(entries[:2])
    assert model.rowCount() == 2
    index = model.index(1)
    assert index.data(model.MessageRole) is entries[1][1]
    assert index.data(model.PlatformRole) is Platform.TWITCH
    assert index.data(model.PlainRole) == "kappa kappa"
    assert index.data(model.IdRole) == "m2"
    assert model.entry_at(1) == entries[1]
    assert model.entry_at(9) is None


# -- searching ------------------------------------------------------------------------


def test_initial_text_runs_search(entries):
    popup = SearchPopup(Settings({}), entries, initial_text="kappa")
    model = popup._results.model()
    assert model.rowCount() == 1
    assert model.entry_at(0)[1].id == "m2"


def test_keystroke_reruns(entries):
    popup = SearchPopup(Settings({}), entries)
    popup._input.setText("kappa")
    assert popup._results.model().rowCount() == 1
    popup._input.setText("hello")
    assert popup._results.model().rowCount() == 1
    assert popup._results.model().entry_at(0)[1].id == "m1"
    popup._input.setText("")
    assert popup._results.model().rowCount() == 0


def test_snapshot_is_immutable(entries):
    popup = SearchPopup(Settings({}), entries, initial_text="hello")
    snapshot_before = list(popup._entries)
    popup._input.setText("kappa")
    assert popup._entries == snapshot_before  # no live appends join the search


def test_predicates_work_end_to_end(entries):
    popup = SearchPopup(Settings({}), entries)
    popup._input.setText("has:link")
    assert [e[1].id for e in popup._entries
            if e[1].id in {m[1].id for m in _model_items(popup)}] == ["m3"]


def _model_items(popup):
    model = popup._results.model()
    return [model.entry_at(row) for row in range(model.rowCount())]


def test_result_cap(entries, monkeypatch):
    from chattersyn.ui import search_popup as mod

    monkeypatch.setattr(mod, "_SEARCH_LIMIT", 2)
    many = [(f"tag{i}", _msg(f"match {i}", f"n{i}")) for i in range(5)]
    popup = SearchPopup(Settings({}), many, initial_text="match")
    assert popup._results.model().rowCount() == 2


# -- go-to ----------------------------------------------------------------------------


def test_go_to_emits_tag_and_message(entries):
    popup = SearchPopup(Settings({}), entries, initial_text="kappa")
    got = []
    popup.go_to_requested.connect(lambda tag, msg: got.append((tag, msg.id)))
    popup._go_to(0)
    assert got == [("tag-a", "m2")]


# -- keys -------------------------------------------------------------------------------


def test_escape_closes(qapp, entries):
    popup = SearchPopup(Settings({}), entries)
    closed = []
    popup.closed.connect(lambda: closed.append(1))
    QTest.keyClick(popup._input, Qt.Key.Key_Escape)
    assert closed == [1]


def test_ctrl_f_selects_all(entries):
    popup = SearchPopup(Settings({}), entries, initial_text="hello")
    popup._input.setCursorPosition(2)
    QTest.keyClick(popup._input, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert popup._input.selectedText() == "hello"


# -- ChatView integration ----------------------------------------------------------------


def test_message_model_row_of(qapp):
    model = MessageModel()
    for i in range(3):
        model.add(_msg(f"m{i}", str(i)))
    model.flush()
    assert model.row_of("1") == 1
    assert model.row_of("missing") is None


def test_scroll_to_message_sets_current(qapp):
    view = ChatView(Settings({}))
    for i in range(5):
        view.message_model.add(_msg(f"m{i}", str(i)))
    view.message_model.flush()
    view.scroll_to_message("3")
    assert view.currentIndex().row() == 3


def test_ctrl_f_emits_search_requested(qapp):
    view = ChatView(Settings({}))
    view.message_model.add(_msg("hello", "m1"))
    view.message_model.flush()
    got = []
    view.search_requested.connect(lambda text, all_tabs: got.append((text, all_tabs)))
    QTest.keyClick(view, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert got == [("", False)]


def test_ctrl_shift_f_marks_all_tabs(qapp):
    view = ChatView(Settings({}))
    got = []
    view.search_requested.connect(lambda text, all_tabs: got.append(all_tabs))
    QTest.keyClick(view, Qt.Key.Key_F,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert got == [True]
