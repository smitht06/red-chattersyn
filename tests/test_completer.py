"""AutoCompletePopup: candidate model, selection reset/wrap, height cap and
Escape. (Visibility-gated keyboard flows are covered with InputWidget.)"""

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from chattersyn.services.autocomplete import CompletionCandidate
from chattersyn.ui.completer import MAX_VISIBLE_ROWS, ROW_HEIGHT, AutoCompletePopup


def _candidates(n):
    return [CompletionCandidate(name=f"c{i}", insert=f"c{i} ") for i in range(n)]


def test_set_candidates_resets_selection_and_range(qapp):
    popup = AutoCompletePopup()
    popup.set_candidates(_candidates(3), 0, 4)
    assert popup.replace_range() == (0, 4)
    assert popup.current().name == "c0"
    popup.move_selection(2)
    assert popup.current().name == "c2"
    popup.set_candidates(_candidates(2), 2, 5)  # every keystroke resets to row 0
    assert popup.replace_range() == (2, 5)
    assert popup.current().name == "c0"


def test_move_selection_wraps_around(qapp):
    popup = AutoCompletePopup()
    popup.set_candidates(_candidates(3), 0, 1)
    popup.move_selection(-1)
    assert popup.current().name == "c2"
    popup.move_selection(1)
    assert popup.current().name == "c0"
    popup.move_selection(2)
    assert popup.current().name == "c2"


def test_empty_popup_has_no_current(qapp):
    popup = AutoCompletePopup()
    popup.set_candidates([], 0, 0)
    assert popup.current() is None
    popup.move_selection(1)  # no-op, must not raise


def test_height_caps_at_max_visible_rows(qapp):
    popup = AutoCompletePopup()
    popup.set_candidates(_candidates(25), 0, 3)
    assert popup._view.height() == MAX_VISIBLE_ROWS * ROW_HEIGHT + 4
    popup.set_candidates(_candidates(3), 0, 3)
    assert popup._view.height() == 3 * ROW_HEIGHT + 4


def test_escape_closes_and_emits_closed(qapp):
    popup = AutoCompletePopup()
    closed = []
    popup.closed.connect(lambda: closed.append(True))
    QTest.keyClick(popup, Qt.Key.Key_Escape)
    assert closed == [True]
