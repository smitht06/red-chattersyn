"""Empty-tab page: replaces the notebook when every tab is closed.

Clickable links replace the blank tab bar area so an empty window still
offers a way forward. Link colors come from QPalette.Link, so both themes
work without extra styling.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

_LINKS = (
    '<a href="combo">new combo tab</a> · '
    '<a href="twitch">new twitch tab</a> · '
    '<a href="youtube">new youtube tab</a>'
)


class EmptyTabPage(QWidget):
    new_combo = Signal()
    new_twitch = Signal()
    new_youtube = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(2)

        heading = QLabel("No chats open", self)
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = heading.font()
        font.setPointSizeF(font.pointSizeF() * 1.6)
        heading.setFont(font)
        layout.addWidget(heading)

        subtitle = QLabel("Add a tab to start watching:", self)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        links = QLabel(_LINKS, self)
        links.setAlignment(Qt.AlignmentFlag.AlignCenter)
        links.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        links.linkActivated.connect(self._on_link)
        layout.addWidget(links)
        layout.addStretch(3)

    def _on_link(self, href: str) -> None:
        if href == "combo":
            self.new_combo.emit()
        elif href == "twitch":
            self.new_twitch.emit()
        elif href == "youtube":
            self.new_youtube.emit()
