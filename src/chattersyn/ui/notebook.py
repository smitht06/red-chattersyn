"""Notebook: movable/closable tab bar with a "+" corner button (Chatterino-style)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTabWidget, QToolButton

from .tab import Tab


class Notebook(QTabWidget):
    new_tab_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDocumentMode(True)
        self.setTabsClosable(True)
        self.setMovable(True)

        add_button = QToolButton(self)
        add_button.setText("+")
        add_button.setAutoRaise(True)
        add_button.setToolTip("New tab (Ctrl+T)")
        add_button.clicked.connect(self.new_tab_requested)
        self.setCornerWidget(add_button, Qt.Corner.TopRightCorner)

    def add_chat_tab(self, tab: Tab, title: str) -> int:
        index = self.addTab(tab, title)
        self.setCurrentIndex(index)
        return index
