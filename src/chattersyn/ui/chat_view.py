"""ChatView: merged chat feed — QListView with batched appends, auto-scroll,
pause-on-hover, link/mention click routing and plain-text copy."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QApplication, QListView, QMenu, QToolTip

from ..config.settings import Settings
from ..models.message import Message
from .message_delegate import MessageDelegate
from .message_model import MessageModel


class ChatView(QListView):
    usercard_requested = Signal(object, str)  # Message, login
    reply_requested = Signal(object)          # Message
    search_requested = Signal(str, bool)      # selection, all_tabs
    mod_action_requested = Signal(object, str)  # Message, action name
    delete_requested = Signal(object)           # Message ("Moderate" submenu)
    pin_requested = Signal(object)              # Message ("Moderate" submenu)

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self._model = MessageModel(settings.get("scrollback", 1000), self)
        self.setModel(self._model)
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

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(100)
        self._flush_timer.timeout.connect(self._flush)
        self._hover_paused = False
        self._hover_item: tuple[str, str] | None = None

    # -- API ---------------------------------------------------------------------

    @property
    def message_model(self) -> MessageModel:
        return self._model

    def add_message(self, msg: Message) -> None:
        self._model.add(msg)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def refresh_visuals(self) -> None:
        self._delegate.clear_cache()
        self.viewport().update()

    def set_moderation_enabled(self, enabled: bool) -> None:
        """Moderation mode ON -> per-row action buttons appear (delegate)."""
        self._delegate.set_moderation_enabled(enabled)

    def set_moderation_actions(self, actions: list[str]) -> None:
        self._delegate.set_moderation_actions(actions)

    def set_youtube_actions(self, actions: list[str]) -> None:
        self._delegate.set_youtube_actions(actions)

    # -- internal ------------------------------------------------------------------

    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _flush(self) -> None:
        added, trimmed = self._model.flush()
        if (added or trimmed) and self._at_bottom() and not self._hover_paused:
            self.scrollToBottom()
        if not self._model._pending:
            self._flush_timer.stop()

    def _tooltip_for(self, hit: tuple[str, str] | None) -> str:
        if hit is None:
            return ""
        kind, payload = hit
        if kind == "link":
            return payload
        if kind == "emote":
            return payload
        return ""

    # -- events ---------------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.viewport():
            if event.type() == QEvent.Type.Enter:
                self._hover_paused = True
            elif event.type() == QEvent.Type.Leave:
                self._hover_paused = False
                self._hover_item = None
                QToolTip.hideText()
                if self._at_bottom():
                    self.scrollToBottom()
            elif event.type() == QEvent.Type.MouseMove:
                self._on_mouse_move(event)
        return False

    def _on_mouse_move(self, event) -> None:
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        hit = None
        if index.isValid():
            hit = self._delegate.hit_test(index, event.position(), self.visualRect(index))
        cursor = Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor
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
                if hit and hit[0] == "mod":
                    msg = self._model.message_at(index.row())
                    if msg is not None:
                        self.mod_action_requested.emit(msg, hit[1])
                    return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
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
        index = self._model.index(row)
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

    def _copy_rows(self, rows: list[int]) -> None:
        lines = []
        for row in rows:
            msg = self._model.message_at(row)
            if msg is not None:
                lines.append(msg.plain)
        QApplication.clipboard().setText("\n".join(lines))
