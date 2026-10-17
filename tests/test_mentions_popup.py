"""MentionsPopup: newest-first rows, live append, go-to navigation, clear."""

import datetime as dt

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from chattersyn.config.settings import Settings
from chattersyn.models.message import Author, Message, Platform
from chattersyn.ui.mentions_popup import MentionsPopup


def _msg(text, msg_id, name="Alice", login="alice"):
    return Message(
        id=msg_id, platform=Platform.TWITCH, channel_id="forsen",
        ts=dt.datetime(2026, 10, 16, 9, 5, 7),
        author=Author(name=name, login=login), plain=text,
    )


@pytest.fixture
def entries(qapp):  # qapp: every popup test constructs widgets
    return [
        ("tag-a", _msg("hello world", "m1")),
        ("tag-a", _msg("kappa kappa", "m2")),
        ("tag-b", _msg("https://x.com link", "m3")),
    ]


def test_initial_entries_in_order(entries):
    popup = MentionsPopup(Settings({}), entries)
    model = popup._results.model()
    assert model.rowCount() == 3
    assert model.entry_at(0) == entries[0]  # caller passes newest first
    assert model.entry_at(2) == entries[2]
    assert model.entry_at(9) is None
    assert popup._label.text() == "Mentions (3)"


def test_empty_state_label(entries):
    popup = MentionsPopup(Settings({}), [])
    assert popup._results.model().rowCount() == 0
    assert popup._label.text() == "Mentions — nothing yet"


def test_append_inserts_front(entries):
    popup = MentionsPopup(Settings({}), entries)
    new_entry = ("tag-a", _msg("fresh hit", "m9"))
    popup.append(*new_entry)
    model = popup._results.model()
    assert model.rowCount() == 4
    assert model.entry_at(0) == new_entry
    assert popup._label.text() == "Mentions (4)"


def test_go_to_emits_tag_and_message(entries):
    popup = MentionsPopup(Settings({}), entries)
    got = []
    popup.go_to_requested.connect(lambda tag, msg: got.append((tag, msg.id)))
    popup._go_to(1)
    assert got == [("tag-a", "m2")]


def test_double_click_goes_to(entries):
    popup = MentionsPopup(Settings({}), entries)
    got = []
    popup.go_to_requested.connect(lambda tag, msg: got.append(msg.id))
    popup._on_double_clicked(popup._results.model().index(0))
    assert got == ["m1"]


def test_clear_button_emits_and_clears(entries):
    popup = MentionsPopup(Settings({}), entries)
    cleared = []
    popup.clear_requested.connect(lambda: cleared.append(1))
    button = popup.findChild(QPushButton)  # the only button in the popup
    assert button is not None
    button.click()
    assert cleared == [1]
    popup.clear()  # MainWindow clears both the store and the popup
    assert popup._results.model().rowCount() == 0
    assert popup._label.text() == "Mentions — nothing yet"


def test_escape_closes(entries):
    popup = MentionsPopup(Settings({}), entries)
    closed = []
    popup.closed.connect(lambda: closed.append(1))
    QTest.keyClick(popup, Qt.Key.Key_Escape)
    assert closed == [1]
