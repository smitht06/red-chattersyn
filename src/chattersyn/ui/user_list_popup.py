"""UserListPopup: the /mods and /vips name list with a filter box.

Names are sorted case-insensitively; typing in the filter keeps only names
containing the text (case-insensitive). Clicking a name emits `user_clicked`.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

from .popup import BasePopup


class UserListPopup(BasePopup):
    user_clicked = Signal(str)

    def __init__(self, title: str, names: list[str], empty_text: str, parent=None):
        super().__init__(parent)
        self.setFixedSize(420, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._title = QLabel(title, self)
        self._title.setStyleSheet("color: #b0a8e8; font-weight: bold;")
        layout.addWidget(self._title)

        self.filter_box = QLineEdit(self)
        self.filter_box.setPlaceholderText("Filter")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_box)

        self.empty_label = QLabel(empty_text, self)
        self.empty_label.setHidden(bool(names))
        layout.addWidget(self.empty_label)

        self.list_widget = QListWidget(self)
        for name in sorted(names, key=str.casefold):
            self.list_widget.addItem(QListWidgetItem(name))
        self.list_widget.itemClicked.connect(lambda item: self.user_clicked.emit(item.text()))
        layout.addWidget(self.list_widget, 1)
        self.list_widget.setHidden(not names)

    def title_text(self) -> str:
        return self._title.text()

    def visible_names(self) -> list[str]:
        return [item.text() for i in range(self.list_widget.count())
                if not (item := self.list_widget.item(i)).isHidden()]

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setHidden(needle not in item.text().casefold())

    def show_at_cursor(self) -> None:
        """Open at the cursor, clamped to the screen it sits on."""
        point = QCursor.pos() + QPoint(8, 8)
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        point.setX(max(geo.left(), min(point.x(), geo.right() - self.width() + 1)))
        point.setY(max(geo.top(), min(point.y(), geo.bottom() - self.height() + 1)))
        self.move(point)
        self.show()
        self.raise_()
