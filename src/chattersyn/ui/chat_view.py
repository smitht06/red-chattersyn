"""ChatView: merged chat feed — QListView with batched appends, auto-scroll,
pause-on-hover, link/mention click routing and plain-text copy.

Auto-scroll keeps an explicit "pinned" intent instead of re-checking "am I at
the bottom?" on every batch: that check went stale whenever the content grew
without a scroll (messages arriving under a hovering mouse, zoom, a narrower
window re-wrapping rows) and the view then never followed again. Now only the
user scrolling UP unpins; reaching the bottom re-pins; while pinned, any
growth of the scroll range is followed. A scrolled-up view jumps back to
the bottom on its own after IDLE_RESUME_MS without user activity in it
(setting general.jump_to_latest_when_idle, read live, default on).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QListView,
    QMenu,
    QPushButton,
    QToolTip,
)

from ..config.settings import Settings
from .message_delegate import MessageDelegate
from .message_model import MessageModel

# Hovering pauses auto-scroll so a name/link doesn't slide away under the
# cursor, but only while the mouse is in use: the pause ends this long after
# the last mouse movement (a mouse left resting on the chat used to freeze it).
HOVER_PAUSE_MS = 1500
# Scrolled up and then left alone (no mouse movement, clicks, wheel, scrollbar
# or keys in the chat) this long -> jump back to the newest messages.
IDLE_RESUME_MS = 15_000
IDLE_RESUME_SETTING = "general.jump_to_latest_when_idle"
BOTTOM_SLACK_PX = 4  # "at the bottom" tolerance

_JUMP_STYLE = (
    "QPushButton { background: rgba(36, 36, 44, 235); color: #e8e8f0;"
    " border: 1px solid #5a5a70; border-radius: 11px; padding: 3px 14px;"
    " font-size: 9pt; }"
    "QPushButton:hover { background: #7a5cff; border-color: #7a5cff; }")


class ChatView(QListView):
    usercard_requested = Signal(object, str)  # Message, login
    reply_requested = Signal(object)          # Message
    search_requested = Signal(str, bool)      # selection, all_tabs
    delete_requested = Signal(object)           # Message ("Moderate" submenu)
    pin_requested = Signal(object)              # Message ("Moderate" submenu)

    def __init__(self, settings: Settings, parent=None, model=None):
        super().__init__(parent)
        self._model = model if model is not None else MessageModel(settings.get("scrollback", 1000), self)
        self.setModel(self._model)
        self._settings = settings
        self._delegate = MessageDelegate(settings, self)
        self.setItemDelegate(self._delegate)

        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self)

        self._hover_paused = False
        self._hover_item: tuple[str, str] | None = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(HOVER_PAUSE_MS)
        self._hover_timer.timeout.connect(self._end_hover_pause)
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(IDLE_RESUME_MS)
        self._idle_timer.timeout.connect(self._on_idle)

        # Follow intent (see module docstring) + rows that arrived while unpinned.
        self._pinned = True
        self._unseen = 0
        bar = self.verticalScrollBar()
        self._last_value = bar.value()
        bar.rangeChanged.connect(self._on_range_changed)
        bar.valueChanged.connect(self._on_value_changed)
        self._model.rowsInserted.connect(self._on_rows_inserted)

        # "↓ N new messages": shown whenever the user has scrolled up.
        self._jump_button = QPushButton(self)
        self._jump_button.setStyleSheet(_JUMP_STYLE)
        self._jump_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._jump_button.setToolTip("Jump to the newest messages")
        self._jump_button.clicked.connect(self.jump_to_latest)
        self._jump_button.hide()

    # -- API ---------------------------------------------------------------------

    @property
    def message_model(self) -> MessageModel:
        return self._model

    def scroll_to_bottom_if_pinned(self) -> None:
        """Auto-scroll after the owning Tab flushes the shared model, unless the
        user has scrolled up or is hovering this pane."""
        if self._pinned and not self._hover_paused:
            self.scrollToBottom()

    def is_pinned(self) -> bool:
        """True while the view follows new messages (the user hasn't scrolled up)."""
        return self._pinned

    def jump_to_latest(self) -> None:
        """Back to the newest message and follow again ("↓ new messages")."""
        self._set_pinned(True)
        self.scrollToBottom()

    def refresh_visuals(self) -> None:
        self._delegate.clear_cache()
        self.viewport().update()

    # -- internal ------------------------------------------------------------------

    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - BOTTOM_SLACK_PX

    def _set_pinned(self, pinned: bool) -> None:
        if pinned:
            self._unseen = 0
        self._pinned = pinned
        if pinned:
            self._idle_timer.stop()
        else:
            self._note_activity()
        self._update_jump_button()

    def _idle_resume_enabled(self) -> bool:
        # Read live: toggling it in Settings applies without re-plumbing views.
        return bool(self._settings.get(IDLE_RESUME_SETTING, True))

    def _note_activity(self) -> None:
        """The user is doing something in the chat: restart the idle countdown
        (only runs while scrolled up, and only when the setting is on)."""
        if not self._pinned and self._idle_resume_enabled():
            self._idle_timer.start()

    def _on_idle(self) -> None:
        # Re-checked here: the setting may have been switched off mid-countdown.
        if not self._pinned and self._idle_resume_enabled():
            self.jump_to_latest()

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        # Content grew or shrank without a scroll (new rows, zoom, re-wrap on
        # resize): keep the bottom in view while following.
        if self._pinned and not self._hover_paused:
            self.verticalScrollBar().setValue(maximum)

    def _on_value_changed(self, value: int) -> None:
        if self._at_bottom():
            self._set_pinned(True)
        elif value < self._last_value:
            self._set_pinned(False)  # only scrolling UP stops the follow
        else:
            self._note_activity()  # scrolling around while scrolled up
        self._last_value = value

    def _on_rows_inserted(self, _parent, first: int, last: int) -> None:
        if not self._pinned:
            self._unseen += last - first + 1
            self._update_jump_button()

    def _update_jump_button(self) -> None:
        button = self._jump_button
        if self._pinned:
            button.hide()
            return
        if self._unseen:
            noun = "message" if self._unseen == 1 else "messages"
            button.setText(f"↓  {self._unseen:,} new {noun}")
        else:
            button.setText("↓  Jump to latest")
        button.adjustSize()
        self._place_jump_button()
        button.show()
        button.raise_()

    def _place_jump_button(self) -> None:
        button = self._jump_button
        area = self.viewport().geometry()
        button.move(area.x() + (area.width() - button.width()) // 2,
                    area.bottom() - button.height() - 8)

    def _start_hover_pause(self) -> None:
        self._hover_paused = True
        self._hover_timer.start()  # (re)start: the pause lasts while the mouse moves

    def _end_hover_pause(self) -> None:
        self._hover_timer.stop()
        self._hover_paused = False
        if self._pinned:
            self.scrollToBottom()  # catch up on whatever arrived during the pause

    def _tooltip_for(self, hit: tuple[str, str] | None) -> str:
        if hit is None:
            return ""
        kind, payload = hit
        if kind == "link":
            return payload
        if kind in ("emote", "badge"):
            return payload
        return ""

    # -- events ---------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        # Row heights depend on width (long messages wrap to more lines as the
        # window narrows). QListView's default Fixed resize mode re-flows only
        # the resized item, leaving rows below at stale y positions, so a row
        # that grew taller overlaps the next one. A delayed items-layout forces
        # a full re-flow; it coalesces the rapid resize events of a drag.
        super().resizeEvent(event)
        self.scheduleDelayedItemsLayout()
        self._place_jump_button()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.viewport():
            if event.type() == QEvent.Type.Enter:
                self._start_hover_pause()
            elif event.type() == QEvent.Type.Leave:
                self._hover_item = None
                QToolTip.hideText()
                self._end_hover_pause()
            elif event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel):
                self._note_activity()
            elif event.type() == QEvent.Type.MouseMove:
                self._note_activity()
                self._start_hover_pause()
                self._on_mouse_move(event)
        return False

    def _on_mouse_move(self, event) -> None:
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        hit = None
        if index.isValid():
            hit = self._delegate.hit_test(index, event.position(), self.visualRect(index))
        # Badges only carry a tooltip — no click action, so no hand cursor.
        clickable = hit is not None and hit[0] != "badge"
        cursor = Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(cursor)
        if hit != self._hover_item:
            self._hover_item = hit
            tooltip = self._tooltip_for(hit)
            if tooltip:
                QToolTip.showText(event.globalPosition().toPoint(), tooltip)
            else:
                QToolTip.hideText()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            index = self.indexAt(pos)
            if index.isValid():
                hit = self._delegate.hit_test(index, event.position(), self.visualRect(index))
                if hit and hit[0] == "link":
                    url = hit[1]
                    if url.lower().startswith(("http://", "https://")):
                        QDesktopServices.openUrl(QUrl(url))
                    return
                if hit and hit[0] == "user":
                    msg = self._model.message_at(index.row())
                    if msg is not None:
                        self.usercard_requested.emit(msg, hit[1])
                    return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        self._note_activity()
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy_selection()
            event.accept()
            return
        if (event.key() == Qt.Key.Key_F
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            all_tabs = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.search_requested.emit(self._selected_plain(), all_tabs)
            event.accept()
            return
        super().keyPressEvent(event)

    def scroll_to_message(self, msg_id: str) -> None:
        """Center a message in the view (search "Go to message")."""
        row = self._model.row_of(msg_id)
        if row is None:
            return
        index = self._model.index(row, 0)
        self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.setCurrentIndex(index)

    def _selected_plain(self) -> str:
        rows = sorted({i.row() for i in self.selectedIndexes()})
        lines = []
        for row in rows:
            msg = self._model.message_at(row)
            if msg is not None:
                lines.append(msg.plain)
        return "\n".join(lines)

    def _copy_selection(self) -> None:
        text = self._selected_plain()
        if text:
            QApplication.clipboard().setText(text)

    def contextMenuEvent(self, event) -> None:
        index = self.indexAt(event.pos())
        rows = sorted({i.row() for i in self.selectedIndexes()})
        if index.isValid() and index.row() not in rows:
            rows = [index.row()]
        menu = QMenu(self)
        if len(rows) == 1:
            msg = self._model.message_at(rows[0])
            if msg is not None and not msg.is_system:
                reply_action = menu.addAction("Reply")
                reply_action.triggered.connect(lambda: self.reply_requested.emit(msg))
                moderate = menu.addMenu("Moderate")
                delete_action = moderate.addAction("Delete message")
                delete_action.triggered.connect(lambda: self.delete_requested.emit(msg))
                pin_action = moderate.addAction("Pin")
                pin_action.triggered.connect(lambda: self.pin_requested.emit(msg))
        if rows:
            copy_action = menu.addAction("Copy")
            copy_action.triggered.connect(lambda: self._copy_rows(rows))
        menu.exec(event.globalPos())
        self._note_activity()  # time spent in the menu doesn't count as idle

    def _copy_rows(self, rows: list[int]) -> None:
        lines = []
        for row in rows:
            msg = self._model.message_at(row)
            if msg is not None:
                lines.append(msg.plain)
        QApplication.clipboard().setText("\n".join(lines))
