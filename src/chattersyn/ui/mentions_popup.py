"""MentionsPopup: the /mentions view over the highlight controller's store.

Rows are the highlighted/mentioned messages themselves, rendered with the
chat's own MessageDelegate, newest first. Unlike search's frozen snapshot,
new hits append live while the popup is open (wired by MainWindow through
the controller's `highlighted` signal). Double-click or right-click -> "Go
to message" scrolls the source tab; Clear empties the controller's store.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListView, QMenu, QPushButton, QVBoxLayout

from ..config.settings import Settings
from ..models.message import Message
from .message_delegate import MessageDelegate
from .popup import BasePopup
from .search_popup import ResultsModel


class MentionsPopup(BasePopup):
    go_to_requested = Signal(object, object)  # owner tag, Message
    clear_requested = Signal()

    def __init__(self, settings: Settings, entries: list[tuple[object, Message]], parent=None):
        super().__init__(parent)
        self.setFixedSize(420, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self._label = QLabel("Mentions", self)
        self._label.setStyleSheet("color: #b0a8e8; font-weight: bold;")
        clear_button = QPushButton("Clear", self)
        clear_button.setToolTip("Clear all mentions")
        clear_button.clicked.connect(self.clear_requested)
        header.addWidget(self._label, 1)
        header.addWidget(clear_button)
        layout.addLayout(header)

        self._results = QListView(self)
        self._results.setModel(ResultsModel(self._results))
        self._results.setItemDelegate(MessageDelegate(settings, self._results))
        self._results.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._results.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._results.doubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self._results, 1)

        if entries:
            self._results.model().set_results(entries)
        self._update_label()

    # -- API ----------------------------------------------------------------------

    def append(self, tag: object, msg: Message) -> None:
        """Live append for hits arriving while the popup is open (newest first)."""
        self._results.model().insert_entry(0, (tag, msg))
        self._update_label()

    def clear(self) -> None:
        self._results.model().set_results([])
        self._update_label()

    # -- internals ------------------------------------------------------------------

    def _update_label(self) -> None:
        count = self._results.model().rowCount()
        self._label.setText(f"Mentions ({count})" if count else "Mentions — nothing yet")

    def _on_double_clicked(self, index: QModelIndex) -> None:
        if index.isValid():
            self._go_to(index.row())

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
