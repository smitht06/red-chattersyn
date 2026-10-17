"""InputWidget completion: emote button, cursor insertion, Tab cycling and
the popup flows (appear, accept on Enter/Tab, Up/Down, Escape, auto-close).

The popup never actually shows here — a test double with fake visibility
stands in for AutoCompletePopup (the suite never opens real windows), so the
visibility-gated eventFilter paths run exactly as in production.
"""

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from chattersyn.config.settings import Settings
from chattersyn.models.emote import Emote
from chattersyn.services.autocomplete import build_candidates
from chattersyn.services.emojis import EmojiEntry
from chattersyn.ui.completer import AutoCompletePopup
from chattersyn.ui.input_widget import InputWidget


class _FakePopup(AutoCompletePopup):
    """Same widget, but visibility is a flag instead of a real window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shown = False

    def show_above(self, anchor):
        self._shown = True

    def isVisible(self):
        return self._shown

    def close(self):
        self._shown = False
        super().close()


def _emote(name):
    return Emote(id=name, name=name, provider="7tv")


def _make_input(monkeypatch, *, users=("forsen",), emotes=(), commands=("/ban",), emojis=()):
    monkeypatch.setattr("chattersyn.ui.input_widget.AutoCompletePopup", _FakePopup)
    widget = InputWidget(Settings())

    def source(text, cursor):
        return build_candidates(
            text, cursor, users=list(users), emotes=list(emotes),
            commands=list(commands), emojis=emojis)

    widget.set_completion_source(source)
    return widget


def _send_spy(widget):
    sent = []
    widget.send_clicked.connect(sent.append)
    return sent


# -- emote button + insertion -------------------------------------------------------------


def test_emote_button_emits_signal(qapp):
    widget = InputWidget(Settings())
    requests = []
    widget.emote_picker_requested.connect(lambda: requests.append(True))
    widget._emote_button.click()
    assert requests == [True]


def test_insert_text_at_cursor(qapp):
    widget = InputWidget(Settings())
    widget.set_text("ab")
    widget.set_cursor_position(1)
    widget.insert_text("X")
    assert widget.text() == "aXb"
    assert widget._edit.cursorPosition() == 2


def test_insert_mention_at_cursor(qapp):
    widget = InputWidget(Settings())
    widget.set_text("")
    widget.insert_mention("Forsen")
    assert widget.text() == "@Forsen "


# -- Tab cycling -------------------------------------------------------------------------


def test_tab_completes_word_of_two_plus_chars(qapp, monkeypatch):
    widget = _make_input(monkeypatch, emotes=[_emote("KEKW"), _emote("KEKWait")])
    widget.set_text("ke")
    QTest.keyClick(widget._edit, Qt.Key.Key_Tab)
    assert widget.text() == "KEKW "
    assert widget._edit.cursorPosition() == 5


def test_tab_cycles_on_repeat_without_text_change(qapp, monkeypatch):
    widget = _make_input(monkeypatch, emotes=[_emote("KEKW"), _emote("KEKWait")])
    widget.set_text("ke")
    assert widget._cycle_tab()          # -> "KEKW ", cursor after it
    widget.set_cursor_position(2)       # back inside the completed word, no text change
    assert widget._cycle_tab()          # next candidate
    assert widget.text() == "KEKWait "


def test_tab_needs_two_char_word(qapp, monkeypatch):
    widget = _make_input(monkeypatch, emotes=[_emote("KEKW")])
    widget.set_text("k")
    QTest.keyClick(widget._edit, Qt.Key.Key_Tab)
    assert widget.text() == "k"


def test_no_source_completion_inert(qapp):
    widget = InputWidget(Settings())
    widget.set_text("@fo")
    assert widget._completion_popup is None
    QTest.keyClick(widget._edit, Qt.Key.Key_Tab)
    assert widget.text() == "@fo"


# -- popup flows ---------------------------------------------------------------------------


def test_popup_appears_for_at_mention(qapp, monkeypatch):
    widget = _make_input(monkeypatch, users=("forsen",))
    widget.set_text("@fo")
    popup = widget._completion_popup
    assert isinstance(popup, _FakePopup) and popup._shown
    assert popup.replace_range() == (0, 3)
    assert popup.current().name == "forsen"


def test_enter_accepts_popup_without_submitting(qapp, monkeypatch):
    widget = _make_input(monkeypatch, users=("forsen",))
    sent = _send_spy(widget)
    widget.set_text("@fo")
    QTest.keyClick(widget._edit, Qt.Key.Key_Return)
    assert widget.text() == "@forsen "
    assert widget._edit.cursorPosition() == 8
    assert sent == []
    assert widget._completion_popup is None  # accepted -> closed


def test_tab_accepts_popup(qapp, monkeypatch):
    widget = _make_input(monkeypatch, users=("forsen",))
    widget.set_text("@fo")
    QTest.keyClick(widget._edit, Qt.Key.Key_Tab)
    assert widget.text() == "@forsen "


def test_up_down_move_popup_selection(qapp, monkeypatch):
    widget = _make_input(monkeypatch, users=("forsen", "foobar"))
    widget.set_text("@fo")
    QTest.keyClick(widget._edit, Qt.Key.Key_Down)
    assert widget._completion_popup.current().name == "foobar"
    QTest.keyClick(widget._edit, Qt.Key.Key_Up)
    assert widget._completion_popup.current().name == "forsen"


def test_escape_closes_popup_without_change(qapp, monkeypatch):
    widget = _make_input(monkeypatch, users=("forsen",))
    widget.set_text("@fo")
    QTest.keyClick(widget._edit, Qt.Key.Key_Escape)
    assert widget._completion_popup is None
    assert widget.text() == "@fo"


def test_popup_auto_closes_when_context_ends(qapp, monkeypatch):
    widget = _make_input(monkeypatch, users=("forsen",))
    widget.set_text("@fo")
    assert widget._completion_popup is not None
    widget.set_text("")
    assert widget._completion_popup is None


def test_colon_popup_serves_emojis_without_emotes(qapp, monkeypatch):
    widget = _make_input(monkeypatch, emojis=(EmojiEntry("joy", "😂", "Smileys & Emotion"),))
    widget.set_text(":jo")
    popup = widget._completion_popup
    assert popup is not None
    assert popup.current().name == "joy"
    assert popup.current().insert == ":joy: "


def test_history_up_still_works_after_completion(qapp, monkeypatch):
    widget = _make_input(monkeypatch)
    widget.set_text("hello")
    widget._submit()
    QTest.keyClick(widget._edit, Qt.Key.Key_Up)
    assert widget.text() == "hello"
