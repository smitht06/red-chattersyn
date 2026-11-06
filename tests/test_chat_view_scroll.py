"""Auto-scroll: the chat follows new messages unless the user scrolled up.

Regressions for the view getting stuck scrolled up: messages arriving under a
hovering mouse, and rows re-wrapping taller (resize/zoom), used to leave the
view above the bottom for good because "pinned" was re-derived from the
scroll position on every batch.
"""

import datetime as dt
from itertools import count

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QEnterEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from chattersyn.config.settings import Settings
from chattersyn.models.message import Author, Message, Platform, TextRun
from chattersyn.ui import theme
from chattersyn.ui.tab import Tab

_ids = count()
LONG = "a fairly long chat message that wraps onto more lines when the window gets narrower"


def _msg(text: str = "hello chat") -> Message:
    return Message(id=f"s{next(_ids)}", platform=Platform.TWITCH, channel_id="ch",
                   ts=dt.datetime.now(), author=Author("bob", login="bob", color="#1e90ff"),
                   runs=[TextRun(text)], plain=text)


@pytest.fixture
def tab(qapp):
    theme.apply(qapp, "dark")
    t = Tab(Settings({}))
    t.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)  # real layout, no window
    t.resize(500, 400)
    t.show()
    yield t
    t.close()


def _pump(tab: Tab, n: int, text: str = "hello chat") -> None:
    for _ in range(n):
        tab.add_message(_msg(text))
    tab._flush()
    QApplication.processEvents()


def _at_bottom(tab: Tab) -> bool:
    bar = tab.view.verticalScrollBar()
    return bar.maximum() > 0 and bar.value() == bar.maximum()


def _enter(tab: Tab) -> None:
    QApplication.sendEvent(tab.view.viewport(),
                           QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))


def _mouse_move() -> QMouseEvent:
    return QMouseEvent(QEvent.Type.MouseMove, QPointF(20, 20), QPointF(20, 20),
                       Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier)


def test_follows_new_messages(tab):
    _pump(tab, 60)
    assert _at_bottom(tab)
    _pump(tab, 10)
    assert _at_bottom(tab)
    assert tab.view.is_pinned()


def test_hover_pause_then_leave_catches_up(tab):
    _pump(tab, 60)
    _enter(tab)
    _pump(tab, 20)
    assert not _at_bottom(tab)  # paused under the mouse
    QApplication.sendEvent(tab.view.viewport(), QEvent(QEvent.Type.Leave))
    assert _at_bottom(tab)
    _pump(tab, 10)
    assert _at_bottom(tab)  # and keeps following afterwards


def test_resting_mouse_pause_expires(tab):
    _pump(tab, 60)
    _enter(tab)
    assert tab.view._hover_timer.isActive()
    _pump(tab, 20)
    tab.view._hover_timer.timeout.emit()  # mouse sat still for HOVER_PAUSE_MS
    assert _at_bottom(tab)
    _pump(tab, 10)
    assert _at_bottom(tab)


def test_rows_growing_taller_keep_following(tab):
    _pump(tab, 60, LONG)
    tab.resize(260, 400)  # narrower: rows re-wrap taller
    QApplication.processEvents()
    _pump(tab, 10, LONG)
    assert _at_bottom(tab)


def test_scroll_up_stops_following_and_counts_new(tab):
    _pump(tab, 60)
    bar = tab.view.verticalScrollBar()
    bar.setValue(bar.value() - 300)
    held = bar.value()
    _pump(tab, 12)
    assert bar.value() == held  # reading history: nothing moves
    assert not tab.view.is_pinned()
    button = tab.view._jump_button
    assert button.isVisible()
    assert button.text() == "↓  12 new messages"


def test_jump_button_returns_and_refollows(tab):
    _pump(tab, 60)
    bar = tab.view.verticalScrollBar()
    bar.setValue(bar.value() - 300)
    _pump(tab, 1)
    assert tab.view._jump_button.text() == "↓  1 new message"
    tab.view._jump_button.click()
    QApplication.processEvents()
    assert _at_bottom(tab)
    assert not tab.view._jump_button.isVisible()
    _pump(tab, 10)
    assert _at_bottom(tab)


def test_scrolling_back_to_bottom_repins(tab):
    _pump(tab, 60)
    bar = tab.view.verticalScrollBar()
    bar.setValue(bar.value() - 300)
    assert not tab.view.is_pinned()
    bar.setValue(bar.maximum())
    assert tab.view.is_pinned()
    assert not tab.view._jump_button.isVisible()
    _pump(tab, 10)
    assert _at_bottom(tab)


def test_scrolling_down_while_paused_keeps_follow(tab):
    _pump(tab, 60)
    _enter(tab)
    _pump(tab, 20)
    bar = tab.view.verticalScrollBar()
    bar.setValue(bar.value() + 40)  # a nudge down that doesn't reach the bottom
    assert tab.view.is_pinned()  # only scrolling UP stops the follow
    tab.view._hover_timer.timeout.emit()
    assert _at_bottom(tab)


# -- idle auto-jump --------------------------------------------------------------------


def _scroll_up(tab: Tab) -> None:
    bar = tab.view.verticalScrollBar()
    bar.setValue(bar.value() - 300)


def test_idle_while_scrolled_up_jumps_to_bottom(tab):
    from chattersyn.ui.chat_view import IDLE_RESUME_MS

    assert IDLE_RESUME_MS == 15_000
    _pump(tab, 60)
    assert not tab.view._idle_timer.isActive()  # following: nothing to resume
    _scroll_up(tab)
    _pump(tab, 12)
    assert tab.view._idle_timer.isActive()
    tab.view._idle_timer.timeout.emit()  # 15 s with no activity
    QApplication.processEvents()
    assert _at_bottom(tab)
    assert tab.view.is_pinned()
    assert not tab.view._jump_button.isVisible()
    assert not tab.view._idle_timer.isActive()


def test_activity_restarts_idle_countdown(tab):
    _pump(tab, 60)
    _scroll_up(tab)
    timer = tab.view._idle_timer
    timer.stop()
    QApplication.sendEvent(tab.view.viewport(), _mouse_move())
    assert timer.isActive()  # mouse movement in the chat counts as activity
    timer.stop()
    _scroll_up(tab)  # scrolling further up does too
    assert timer.isActive()


def test_returning_to_bottom_cancels_idle_countdown(tab):
    _pump(tab, 60)
    _scroll_up(tab)
    assert tab.view._idle_timer.isActive()
    tab.view.jump_to_latest()
    assert not tab.view._idle_timer.isActive()


def test_idle_jump_setting_defaults_on(qapp):
    assert Settings({}).get("general.jump_to_latest_when_idle") is True


def test_idle_jump_can_be_switched_off(tab):
    tab._settings.set("general.jump_to_latest_when_idle", False)
    _pump(tab, 60)
    _scroll_up(tab)
    _pump(tab, 12)
    assert not tab.view._idle_timer.isActive()  # no countdown while off
    held = tab.view.verticalScrollBar().value()
    tab.view._on_idle()  # even a stray timeout must not jump
    assert tab.view.verticalScrollBar().value() == held
    assert not tab.view.is_pinned()


def test_idle_jump_switched_off_mid_countdown(tab):
    _pump(tab, 60)
    _scroll_up(tab)
    assert tab.view._idle_timer.isActive()  # counting down (default on)
    tab._settings.set("general.jump_to_latest_when_idle", False)
    tab.view._idle_timer.timeout.emit()
    assert not tab.view.is_pinned()  # stayed where the user was reading
