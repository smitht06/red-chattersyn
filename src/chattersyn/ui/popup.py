"""BasePopup: dark, frameless top-level popup shared by the search popup,
usercard and (later) the autocomplete/emote-picker popups.

Qt.Popup gives click-outside-to-close; closed is emitted for both that path
and Escape so owners can drop their reference.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QFrame

_POPUP_STYLE = (
    "QFrame#popupRoot { background: #1e1e1e; border: 1px solid #3a3a3a;"
    " border-radius: 6px; }"
)


class BasePopup(QFrame):
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("popupRoot")
        self.setStyleSheet(_POPUP_STYLE)

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def show_near(self, anchor) -> None:
        """Show the popup centered over `anchor` (a QWidget)."""
        self.adjustSize()
        point = anchor.mapToGlobal(anchor.rect().center())
        point -= QPoint(self.width() // 2, self.height() // 2)
        self.move(point)
        self.show()
        self.raise_()
