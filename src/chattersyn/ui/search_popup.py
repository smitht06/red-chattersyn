"""SearchPopup: Chatterino-style Ctrl+F search over a fixed message snapshot.

The snapshot (list of (owner_tag, Message) pairs) is built ONCE when the
popup opens — messages arriving after open are excluded (parity). Every
keystroke re-runs the engine over the snapshot; results render with the
chat's own MessageDelegate. Right-click -> "Go to message" scrolls the
source view. Escape closes; Ctrl+F refocuses the input and selects all;
PgUp/PgDn scroll the results.

Result rows are never logged (the logger only subscribes bridge.message).
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QObject, Qt, Signal
from PySide6.QtWidgets import QLineEdit, QListView, QMenu, QVBoxLayout

from ..config.settings import Settings
from ..models.message import Message
from ..services.search import SearchEngine
from .message_delegate import MessageDelegate
from .message_model import MessageModel
from .popup import BasePopup

_SEARCH_LIMIT = 1000  # results capped at the scrollback limit (parity)


class ResultsModel(QAbstractListModel):
    """Thin model exposing Messages through the chat model's roles, so the
    MessageDelegate renders search/mentions rows exactly like chat rows."""

    MessageRole = MessageModel.MessageRole
    PlatformRole = MessageModel.PlatformRole
    PlainRole = MessageModel.PlainRole
    IdRole = MessageModel.IdRole

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[tuple[object, Message]] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        msg = self._items[index.row()][1]
        if role == self.MessageRole:
            return msg
        if role == self.PlatformRole:
            return msg.platform
        if role == self.PlainRole:
            return msg.plain
        if role == self.IdRole:
            return msg.id
        return None

    def entry_at(self, row: int) -> tuple[object, Message] | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def set_results(self, entries: list[tuple[object, Message]]) -> None:
        self.beginResetModel()
        self._items = entries
        self.endResetModel()

    def insert_entry(self, row: int, entry: tuple[object, Message]) -> None:
        """Live append (the /mentions view grows while open)."""
        row = max(0, min(row, len(self._items)))
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.insert(row, entry)
        self.endInsertRows()


class SearchPopup(BasePopup):
    go_to_requested = Signal(object, object)  # owner tag, Message

    def __init__(self, settings: Settings, entries: list[tuple[object, Message]],
                 initial_text: str = "", parent=None):
        super().__init__(parent)
        self._engine = SearchEngine()
        self._entries = entries  # the immutable snapshot
        self.setFixedSize(420, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._input = QLineEdit(self)
        self._input.setPlaceholderText("Search messages…")
        self._input.setText(initial_text)
        self._input.setClearButtonEnabled(True)
        self._input.textChanged.connect(self._rerun)
        self._input.installEventFilter(self)
        layout.addWidget(self._input)

        self._results = QListView(self)
        self._results.setModel(ResultsModel(self._results))
        self._results.setItemDelegate(MessageDelegate(settings, self._results))
        self._results.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._results.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._results.setMouseTracking(True)
        layout.addWidget(self._results, 1)

        self._rerun(initial_text)

    # -- API ----------------------------------------------------------------------

    def focus_input(self) -> None:
        self._input.setFocus()
        self._input.selectAll()

    # -- internals ------------------------------------------------------------------

    def _rerun(self, text: str) -> None:
        if not text.strip():
            self._results.model().set_results([])
            return
        snapshot = [msg for _, msg in self._entries]
        matched_ids = {msg.id for msg in self._engine.search(snapshot, text)}
        entries = [entry for entry in self._entries if entry[1].id in matched_ids]
        self._results.model().set_results(entries[:_SEARCH_LIMIT])

    def _go_to(self, row: int) -> None:
        entry = self._results.model().entry_at(row)
        if entry is not None:
            self.go_to_requested.emit(entry[0], entry[1])
            self.close()

    def contextMenuEvent(self, event) -> None:
        index = self._results.indexAt(self._results.viewport().mapFrom(self, event.pos()))
        if not index.isValid():
            return
        menu = QMenu(self)
        menu.addAction("Go to message", lambda: self._go_to(index.row()))
        menu.exec(event.globalPos())

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.close()
                return True
            if (event.key() == Qt.Key.Key_F
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._input.selectAll()
                return True
            if event.key() in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                page = 1 if event.key() == Qt.Key.Key_PageDown else -1
                bar = self._results.verticalScrollBar()
                bar.setValue(bar.value() + page * bar.pageStep())
                return True
        return False
