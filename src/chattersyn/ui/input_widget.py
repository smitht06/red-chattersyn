"""InputWidget: the single input bar that posts to both chats.

Dual char counters (Twitch 500 codepoints / YouTube 200 chars) with
per-platform color states, history, Enter to send, and a chip row driven
by SendResult transitions ("Twitch ✓ · YouTube ⏳ retrying in 3 s [cancel]").

Autocomplete: a DontFocus popup floats above the input for `@`/`:` words
(Enter/Tab accept, Up/Down wrap, Escape closes) and Tab cycles candidates
for any word (word >= 2 chars). The candidate source is injected by
MainWindow per tab; without one, completion is inert.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..config.settings import Settings
from ..core.text import TWITCH_MESSAGE_LIMIT, YOUTUBE_MESSAGE_LIMIT
from ..services.autocomplete import CompletionResult
from .completer import AutoCompletePopup

log = logging.getLogger(__name__)

_CHIP_STYLE = "border-radius: 3px; padding: 1px 6px; font-size: 9pt;"
_CHIP_KIND_COLORS = {
    "ok": ("#1e3a1e", "#7fd47f"),
    "pending": ("#3a341e", "#d4c97f"),
    "error": ("#3a1e1e", "#d47f7f"),
    "info": ("#232323", "#9a9a9a"),
}


class _Chip(QWidget):
    """One platform's send-status chip: label + optional [cancel] button."""

    def __init__(self, text: str, kind: str, cancel: Callable[[], None] | None = None, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.kind = kind
        self._label = QLabel(text, self)
        self._set_kind(kind)
        layout.addWidget(self._label)
        if cancel is not None:
            button = QPushButton("cancel", self)
            button.setStyleSheet(
                "QPushButton { background: transparent; color: #d47f7f; border: none;"
                " font-size: 9pt; }")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(cancel)
            layout.addWidget(button)

    def _set_kind(self, kind: str) -> None:
        background, color = _CHIP_KIND_COLORS.get(kind, _CHIP_KIND_COLORS["info"])
        self._label.setStyleSheet(
            f"background: {background}; color: {color}; {_CHIP_STYLE}")


class InputWidget(QWidget):
    send_clicked = Signal(str)
    emote_picker_requested = Signal()

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._tw_limit = TWITCH_MESSAGE_LIMIT
        self._history: list[str] = []
        self._history_index = 0
        self._chips: dict[str, _Chip] = {}
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_ok_chips)
        self._completion_source: Callable[[str, int], CompletionResult | None] | None = None
        self._completion: CompletionResult | None = None
        self._tab_cycle_index = 0
        self._applying_candidate = False
        self._completion_popup: AutoCompletePopup | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 6)
        outer.setSpacing(2)

        self._chips_row = QHBoxLayout()
        self._chips_row.setContentsMargins(0, 0, 0, 0)
        self._chips_row.setSpacing(6)
        self._chips_row.addStretch(0)
        outer.addLayout(self._chips_row)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText("Send a message — it goes to BOTH chats")
        self._edit.setClearButtonEnabled(True)
        self._edit.returnPressed.connect(self._submit)
        self._edit.textChanged.connect(self._update_counter)
        self._edit.textChanged.connect(self._on_text_changed)
        self._edit.cursorPositionChanged.connect(self._on_cursor_moved)
        self._edit.installEventFilter(self)

        self._emote_button = QPushButton("🙂", self)
        self._emote_button.setToolTip("Emote picker (Ctrl+E)")
        self._emote_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._emote_button.clicked.connect(self.emote_picker_requested)

        self._counter = QLabel(self)
        self._counter.setTextFormat(Qt.TextFormat.RichText)
        self._counter.setStyleSheet("font-size: 9pt;")

        self._send_button = QPushButton("Send", self)
        self._send_button.setDefault(True)
        self._send_button.clicked.connect(self._submit)

        row.addWidget(self._edit, 1)
        row.addWidget(self._emote_button)
        row.addWidget(self._counter)
        row.addWidget(self._send_button)
        outer.addLayout(row)

        self._update_counter()

    # -- API ----------------------------------------------------------------------

    def text(self) -> str:
        return self._edit.text()

    def set_text(self, text: str) -> None:
        self._edit.setText(text)
        self._edit.setFocus()

    def insert_mention(self, name: str) -> None:
        """Insert '@name ' at the cursor (YouTube-style plain mentions)."""
        self.insert_text(f"@{name} ")

    def insert_text(self, text: str) -> None:
        """Insert `text` at the cursor (emote picker / mention / reply)."""
        pos = self._edit.cursorPosition()
        current = self._edit.text()
        self._edit.setText(current[:pos] + text + current[pos:])
        self._edit.setCursorPosition(pos + len(text))
        self._edit.setFocus()

    def set_cursor_position(self, pos: int) -> None:
        self._edit.setCursorPosition(pos)

    def focus_input(self) -> None:
        self._edit.setFocus()

    def set_completion_source(self, source: Callable[[str, int], CompletionResult | None]) -> None:
        """(text, cursor) -> CompletionResult for the word under the cursor."""
        self._completion_source = source

    def show_send_chip(self, platform: str, text: str, kind: str,
                       cancel: Callable[[], None] | None = None) -> None:
        """Add/replace a platform's send-status chip. kind: ok/pending/error/info."""
        old = self._chips.pop(platform, None)
        if old is not None:
            old.deleteLater()
        chip = _Chip(text, kind, cancel)
        self._chips[platform] = chip
        self._chips_row.insertWidget(self._chips_row.count() - 1, chip)
        if kind == "ok":
            # "✓ sent" chips linger a moment, then disappear on their own.
            self._hide_timer.start(4000)

    def hide_send_chip(self, platform: str) -> None:
        chip = self._chips.pop(platform, None)
        if chip is not None:
            chip.deleteLater()

    def clear_send_chips(self) -> None:
        for platform in list(self._chips):
            self.hide_send_chip(platform)

    def set_twitch_limit(self, limit: int) -> None:
        """Dual sends cap both platforms at the stricter (YouTube) limit;
        single-Twitch sends restore Twitch's own 500."""
        self._tw_limit = limit
        self._update_counter()

    # -- completion -----------------------------------------------------------------

    def _on_text_changed(self, _text: str) -> None:
        if self._applying_candidate:
            return  # our own completion insert — keep the Tab-cycle position
        self._tab_cycle_index = 0
        self._update_completion()

    def _on_cursor_moved(self, _old: int, _new: int) -> None:
        self._update_completion()

    def _update_completion(self) -> None:
        if self._completion_source is None:
            return
        self._completion = self._completion_source(
            self._edit.text(), self._edit.cursorPosition())
        if (self._completion is not None and self._completion.popup
                and self._completion.candidates):
            popup = self._ensure_popup()
            popup.set_candidates(self._completion.candidates,
                                 self._completion.start, self._completion.end)
            popup.show_above(self._edit)
        else:
            self._close_popup()

    def _ensure_popup(self) -> AutoCompletePopup:
        if self._completion_popup is None:
            self._completion_popup = AutoCompletePopup()
            self._completion_popup.closed.connect(self._on_popup_closed)
        return self._completion_popup

    def _close_popup(self) -> None:
        if self._completion_popup is not None:
            self._completion_popup.close()

    def _on_popup_closed(self) -> None:
        self._completion_popup = None

    def _popup_visible(self) -> bool:
        return self._completion_popup is not None and self._completion_popup.isVisible()

    def _accept_popup(self) -> bool:
        popup = self._completion_popup
        if popup is None or not popup.isVisible():
            return False
        candidate = popup.current()
        start, end = popup.replace_range()
        self._close_popup()
        if candidate is not None:
            self._apply_candidate(start, end, candidate)
        return True

    def _cycle_tab(self) -> bool:
        """Tab without the popup: complete the current word, cycling on repeat."""
        if self._completion_source is None:
            return False
        self._update_completion()
        if self._completion is None or not self._completion.candidates:
            return False
        if self._completion.end - self._completion.start < 2:
            return False  # Tab cycling needs word >= 2 chars (parity)
        candidate = self._completion.candidates[
            self._tab_cycle_index % len(self._completion.candidates)]
        self._tab_cycle_index += 1
        self._apply_candidate(self._completion.start, self._completion.end, candidate)
        return True

    def _apply_candidate(self, start: int, end: int, candidate) -> None:
        text = self._edit.text()
        end = min(max(end, start), len(text))
        if candidate.insert.endswith(" ") and end < len(text) and text[end] == " ":
            end += 1  # collapse the space left behind by the replaced word
        self._applying_candidate = True
        try:
            self._edit.setText(text[:start] + candidate.insert + text[end:])
            self._edit.setCursorPosition(start + len(candidate.insert))
        finally:
            self._applying_candidate = False

    # -- internals ------------------------------------------------------------------

    def _hide_ok_chips(self) -> None:
        for platform, chip in list(self._chips.items()):
            if chip.kind == "ok":  # "sent" chips only — errors stay visible
                self.hide_send_chip(platform)

    def _update_counter(self) -> None:
        n = len(self._edit.text())
        tw_limit = self._tw_limit
        yt_limit = YOUTUBE_MESSAGE_LIMIT
        tw_color = "#ff5555" if n > tw_limit else ("#ffaa44" if n >= tw_limit - 20 else "#8a8a8a")
        yt_color = "#ff5555" if n > yt_limit else ("#ffaa44" if n >= yt_limit - 20 else "#8a8a8a")
        self._counter.setText(
            f'<span style="color:{tw_color}">Tw {n}/{tw_limit}</span> · '
            f'<span style="color:{yt_color}">Yt {n}/{yt_limit}</span>')

    def _submit(self) -> None:
        text = self._edit.text().strip()
        if not text:
            return
        if self._history and self._history[-1] != text or not self._history:
            self._history.append(text)
        self._history_index = len(self._history)
        self._edit.clear()
        self.send_clicked.emit(text)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if self._popup_visible():
                if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                    self._completion_popup.move_selection(
                        -1 if key == Qt.Key.Key_Up else 1)
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                    return self._accept_popup()  # accept, don't send
                if key == Qt.Key.Key_Escape:
                    self._close_popup()
                    return True
            elif key == Qt.Key.Key_Tab and self._cycle_tab():
                return True
            if key == Qt.Key.Key_Up and not self._history:
                return True
            if key == Qt.Key.Key_Up and self._history:
                self._history_index = max(0, self._history_index - 1)
                self._edit.setText(self._history[self._history_index])
                self._edit.setCursorPosition(len(self._edit.text()))
                return True
            if key == Qt.Key.Key_Down and self._history:
                self._history_index = min(len(self._history), self._history_index + 1)
                text = self._history[self._history_index] if self._history_index < len(self._history) else ""
                self._edit.setText(text)
                self._edit.setCursorPosition(len(text))
                return True
        return False
