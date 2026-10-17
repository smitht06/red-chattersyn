"""Chatterino-style theming: Fusion style + hand-rolled QPalette (dark/light).

Widgets that cache rendering internals (the message delegate) connect to
`theme_notifier.changed` and rebuild their caches when the theme changes.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


class _ThemeNotifier(QObject):
    changed = Signal()


theme_notifier = _ThemeNotifier()

BASE_FONT_PT = 11

DARK = {
    QPalette.ColorRole.Window: "#1e1e1e",
    QPalette.ColorRole.WindowText: "#dedede",
    QPalette.ColorRole.Base: "#262626",
    QPalette.ColorRole.AlternateBase: "#2b2b2b",
    QPalette.ColorRole.Text: "#dedede",
    QPalette.ColorRole.Button: "#2f2f2f",
    QPalette.ColorRole.ButtonText: "#dedede",
    QPalette.ColorRole.BrightText: "#ff5555",
    QPalette.ColorRole.Highlight: "#7a5cff",
    QPalette.ColorRole.HighlightedText: "#ffffff",
    QPalette.ColorRole.Link: "#4f8cff",
    QPalette.ColorRole.ToolTipBase: "#2f2f2f",
    QPalette.ColorRole.ToolTipText: "#dedede",
    QPalette.ColorRole.PlaceholderText: "#7a7a7a",
}

LIGHT = {
    QPalette.ColorRole.Window: "#f2f2f2",
    QPalette.ColorRole.WindowText: "#1e1e1e",
    QPalette.ColorRole.Base: "#ffffff",
    QPalette.ColorRole.AlternateBase: "#f7f7f7",
    QPalette.ColorRole.Text: "#1e1e1e",
    QPalette.ColorRole.Button: "#e4e4e4",
    QPalette.ColorRole.ButtonText: "#1e1e1e",
    QPalette.ColorRole.BrightText: "#cc0000",
    QPalette.ColorRole.Highlight: "#7a5cff",
    QPalette.ColorRole.HighlightedText: "#ffffff",
    QPalette.ColorRole.Link: "#0057cc",
    QPalette.ColorRole.ToolTipBase: "#ffffff",
    QPalette.ColorRole.ToolTipText: "#1e1e1e",
    QPalette.ColorRole.PlaceholderText: "#8a8a8a",
}


def build_palette(theme: str) -> QPalette:
    colors = DARK if theme == "dark" else LIGHT
    palette = QPalette()
    for role, value in colors.items():
        palette.setColor(role, QColor(value))
    palette.setColor(QPalette.ColorRole.Mid, palette.color(QPalette.ColorRole.Base))
    palette.setColor(QPalette.ColorRole.Midlight, palette.color(QPalette.ColorRole.Button))
    palette.setColor(QPalette.ColorRole.Dark, QColor("#141414" if theme == "dark" else "#c8c8c8"))
    palette.setColor(QPalette.ColorRole.Light, QColor("#3c3c3c" if theme == "dark" else "#ffffff"))
    palette.setColor(QPalette.ColorRole.Shadow, QColor("#0f0f0f" if theme == "dark" else "#999999"))
    return palette


def apply(app: QApplication, theme: str, ui_scale: float = 1.0) -> None:
    app.setStyle("Fusion")
    app.setPalette(build_palette(theme))
    font = app.font()
    font.setPointSizeF(BASE_FONT_PT * ui_scale)
    app.setFont(font)
    theme_notifier.changed.emit()
