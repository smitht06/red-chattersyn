"""TabHeader: pair summary (Twitch channel + YouTube stream), status and
per-platform send-toggle chips [Tw✓][Yt✓]."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ..config.settings import Settings

TWITCH_DOT = '<span style="color:#9146ff">●</span>'
YOUTUBE_DOT = '<span style="color:#e62117">●</span>'
GRAY_DOT = '<span style="color:#666666">●</span>'

_CHIP_ON = """
QPushButton { background: #2d2d2d; color: #dddddd; border: 1px solid #555555;
              border-radius: 8px; padding: 1px 8px; font-size: 9pt; }
"""
_CHIP_OFF = """
QPushButton { background: #232323; color: #6a6a6a; border: 1px solid #333333;
              border-radius: 8px; padding: 1px 8px; font-size: 9pt; }
"""
_SHIELD_ON = """
QPushButton { background: #1e3a1e; color: #7fd47f; border: 1px solid #2a5a2a;
              border-radius: 8px; padding: 1px 8px; font-size: 10pt; }
"""
_SHIELD_DISABLED = """
QPushButton { background: #232323; color: #4a4a4a; border: 1px solid #333333;
              border-radius: 8px; padding: 1px 8px; font-size: 10pt; }
"""


class TabHeader(QWidget):
    send_toggle_changed = Signal(str, bool)  # platform, enabled
    moderation_toggled = Signal(bool)  # shield on/off (per-tab moderation mode)

    def __init__(self, settings: Settings, twitch_channel: str = "", youtube_title: str = "", parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setObjectName("tabHeader")
        self.setStyleSheet("QWidget#tabHeader { background: #232323; border-bottom: 1px solid #141414; }")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(10)

        self._twitch_label = QLabel(self)
        self._twitch_label.setTextFormat(Qt.TextFormat.RichText)

        self._youtube_label = QLabel(self)
        self._youtube_label.setTextFormat(Qt.TextFormat.RichText)

        self._twitch_chip = QPushButton("Tw ✓", self)
        self._twitch_chip.setCheckable(True)
        self._twitch_chip.setChecked(True)
        self._twitch_chip.setToolTip("Send to Twitch (toggle)")
        self._twitch_chip.setStyleSheet(_CHIP_ON)
        self._twitch_chip.toggled.connect(
            lambda on: self._on_chip("twitch", on))

        self._youtube_chip = QPushButton("Yt ✓", self)
        self._youtube_chip.setCheckable(True)
        self._youtube_chip.setChecked(True)
        self._youtube_chip.setToolTip("Send to YouTube (toggle)")
        self._youtube_chip.setStyleSheet(_CHIP_ON)
        self._youtube_chip.toggled.connect(
            lambda on: self._on_chip("youtube", on))
        # Programmatic send block (quota exhausted / auth required): the
        # chip is unchecked + disabled; unblocking restores the user's
        # pre-block preference instead of forcing the toggle back on.
        self._youtube_blocked = False
        self._youtube_pre_block = True
        self._youtube_block_reason = ""

        self._status_label = QLabel("", self)
        self._status_label.setStyleSheet("color: #8a8a8a; font-size: 9pt;")

        # Chat-mode indicator (ROOMSTATE-driven; docs/m4.md moderation mode).
        self._modes_label = QLabel("", self)
        self._modes_label.setStyleSheet("color: #a0a0c0; font-size: 8.5pt;")
        self._modes_label.hide()

        self._shield = QPushButton("🛡", self)
        self._shield.setCheckable(True)
        self._shield.setToolTip("Moderation mode — show action buttons on messages")
        self._shield.setEnabled(False)
        self._shield.setStyleSheet(_SHIELD_DISABLED)
        self._shield.toggled.connect(self._on_shield_toggled)

        layout.addWidget(self._twitch_label)
        layout.addWidget(self._youtube_label)
        layout.addWidget(self._modes_label)
        layout.addStretch(1)
        layout.addWidget(self._shield)
        layout.addWidget(self._twitch_chip)
        layout.addWidget(self._youtube_chip)
        layout.addWidget(self._status_label)

        self.set_twitch(twitch_channel, live=False)
        self.set_youtube(youtube_title, live=False)

    # -- API ----------------------------------------------------------------------

    def set_twitch(self, channel: str, live: bool = False) -> None:
        dot = TWITCH_DOT if live else GRAY_DOT
        name = channel or "—"
        self._twitch_label.setText(f'{dot} Twitch: <b>{name}</b>')

    def set_youtube(self, title: str, live: bool = False) -> None:
        dot = YOUTUBE_DOT if live else GRAY_DOT
        label = title or "—"
        self._youtube_label.setText(f"{dot} YouTube: <i>{label}</i>")

    def set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def set_chat_modes(self, text: str) -> None:
        """Compact ROOMSTATE summary ("slow 30s · subs-only") next to the
        channel labels; empty text hides the indicator. YouTube has no
        in-feed chat-mode signal (docs/m4.md), so this stays Twitch-only."""
        self._modes_label.setText(text)
        self._modes_label.setVisible(bool(text))

    def twitch_enabled(self) -> bool:
        return self._twitch_chip.isChecked()

    def youtube_enabled(self) -> bool:
        return self._youtube_chip.isChecked()

    def set_youtube_send_enabled(self, enabled: bool, reason: str = "") -> None:
        """Block/unblock the YouTube send chip. A blocked chip is unchecked
        and disabled with the reason as tooltip; unblocking restores the
        pre-block checked state."""
        chip = self._youtube_chip
        if not enabled:
            if not self._youtube_blocked:
                self._youtube_pre_block = chip.isChecked()
            self._youtube_blocked = True
            self._youtube_block_reason = reason
            chip.setChecked(False)  # toggled -> "Yt ✗" style + send limit sync
            chip.setEnabled(False)
            chip.setToolTip(reason or "YouTube send unavailable")
        else:
            self._youtube_blocked = False
            self._youtube_block_reason = ""
            chip.setEnabled(True)
            chip.setChecked(self._youtube_pre_block)
            chip.setToolTip("Send to YouTube (toggle)")

    def youtube_send_blocked(self) -> bool:
        return self._youtube_blocked

    # -- moderation -------------------------------------------------------------

    def set_moderation_available(self, available: bool) -> None:
        """Enable the shield when the account has mod rights on either
        platform of the pair (or when rights are unknown)."""
        self._shield.setEnabled(available)
        if not available:
            self._shield.setChecked(False)  # no rights -> mode off
        self._update_shield_style()

    def moderation_enabled(self) -> bool:
        return self._shield.isChecked() and self._shield.isEnabled()

    def _update_shield_style(self) -> None:
        if not self._shield.isEnabled():
            self._shield.setStyleSheet(_SHIELD_DISABLED)
        else:
            self._shield.setStyleSheet(_SHIELD_ON if self._shield.isChecked() else _CHIP_ON)

    def _on_chip(self, platform: str, checked: bool) -> None:
        chip = self._twitch_chip if platform == "twitch" else self._youtube_chip
        chip.setText(f"{'Tw' if platform == 'twitch' else 'Yt'} {'✓' if checked else '✗'}")
        chip.setStyleSheet(_CHIP_ON if checked else _CHIP_OFF)
        self.send_toggle_changed.emit(platform, checked)

    def _on_shield_toggled(self, checked: bool) -> None:
        self._update_shield_style()
        self.moderation_toggled.emit(checked)
