"""Settings dialog: left-nav page list + stacked pages (Chatterino-style).

The dialog edits a snapshot of the settings and writes everything back in
accept(): `settings.set_batch` (which saves settings.json) plus the
commands.json file. MainWindow applies the visual fallout (theme, scale,
scrollback, hotkeys, reloaded controllers) after exec() returns Accepted.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import Settings
from ..core.image_cache import image_cache
from ..models.message import Author, Message, Platform
from ..services.commands import UserCommand, load_user_commands, save_user_commands
from ..services.emotes.youtube_emoji import DEFAULT_EMOJI, emoji_map
from ..services.highlights import HighlightMatcher

VERSION = "0.1.0"

HOTKEY_DEFAULTS = {
    "open_settings": "Ctrl+,",
    "new_tab": "Ctrl+T",
    "close_tab": "Ctrl+W",
    "next_tab": "Ctrl+Tab",
    "prev_tab": "Ctrl+Shift+Tab",
    "focus_input": "Ctrl+L",
    "toggle_search": "Ctrl+F",
    "toggle_theme": "",
    "emote_picker": "Ctrl+E",
    "zoom_in": "Ctrl+=",
    "zoom_out": "Ctrl+-",
    "zoom_reset": "Ctrl+0",
}

HOTKEY_LABELS = {
    "open_settings": "Open settings",
    "new_tab": "New tab",
    "close_tab": "Close tab",
    "next_tab": "Next tab",
    "prev_tab": "Previous tab",
    "focus_input": "Focus message input",
    "toggle_search": "Search messages",
    "toggle_theme": "Toggle light/dark theme",
    "emote_picker": "Open emote picker",
    "zoom_in": "Zoom in",
    "zoom_out": "Zoom out",
    "zoom_reset": "Reset zoom",
}

_NAV_STYLE = """
QListWidget { background: #1b1b1b; border: none; font-size: 10pt; }
QListWidget::item { padding: 8px 14px; color: #cccccc; }
QListWidget::item:selected { background: #33334d; color: #ffffff; }
"""


class _KeyCaptureDialog(QDialog):
    """Grabs the next key combination (Esc cancels, Backspace clears)."""

    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Shortcut")
        self._sequence = ""
        self._done = False  # set once a key (or Backspace) has been captured
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"Current: {current or '(none)'}\n\n"
            "Press the new key combination.\n"
            "Esc cancels — Backspace clears the shortcut.", self))
        self._captured = QLabel("…", self)
        self._captured.setStyleSheet("font-weight: bold; padding: 6px;")
        layout.addWidget(self._captured)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._accept_sequence)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_sequence(self) -> None:
        if not self._done:
            return  # nothing captured yet
        self.accept()

    def sequence(self) -> str:
        return self._sequence

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.reject()
            return
        if key == Qt.Key.Key_Backspace:
            self._sequence = ""  # clear (an empty result still counts as chosen)
            self._done = True
            self._captured.setText("(cleared — press again to set)")
            return
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift,
                   Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            return  # modifiers alone are not a shortcut
        text = QKeySequence(event.keyCombination()).toString()
        if not text:
            return
        self._sequence = text
        self._done = True
        self._captured.setText(text)


class _HighlightEditor(QDialog):
    """Add/edit one highlight rule."""

    def __init__(self, entry: dict | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Highlight")
        entry = entry or {"text": "", "regex": False, "case_sensitive": False,
                          "color": "#7a5cff", "alert": True, "sound": False}
        self._color = entry.get("color") or "#7a5cff"
        form = QFormLayout(self)
        self._text = QLineEdit(str(entry.get("text") or ""), self)
        self._regex = QCheckBox("Treat as regular expression", self)
        self._regex.setChecked(bool(entry.get("regex")))
        self._case = QCheckBox("Case sensitive", self)
        self._case.setChecked(bool(entry.get("case_sensitive")))
        self._color_button = QPushButton(self._color, self)
        self._color_button.clicked.connect(self._pick_color)
        self._alert = QCheckBox("Show in /mentions", self)
        self._alert.setChecked(bool(entry.get("alert")))
        self._sound = QCheckBox("Play sound", self)
        self._sound.setChecked(bool(entry.get("sound")))
        form.addRow("Text:", self._text)
        form.addRow("", self._regex)
        form.addRow("", self._case)
        form.addRow("Color:", self._color_button)
        form.addRow("", self._alert)
        form.addRow("", self._sound)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _pick_color(self) -> None:
        color = QColorDialog.getColor()
        if color.isValid():
            self._color = color.name()
            self._color_button.setText(self._color)

    def entry(self) -> dict:
        return {
            "text": self._text.text().strip(),
            "regex": self._regex.isChecked(),
            "case_sensitive": self._case.isChecked(),
            "color": self._color,
            "alert": self._alert.isChecked(),
            "sound": self._sound.isChecked(),
        }


class _TextPairEditor(QDialog):
    """Add/edit one (name, template) pair (commands, moderation actions)."""

    def __init__(self, title: str, name_label: str, value_label: str,
                 name: str = "", value: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        form = QFormLayout(self)
        self._name = QLineEdit(name, self)
        self._value = QLineEdit(value, self)
        form.addRow(f"{name_label}:", self._name)
        form.addRow(f"{value_label}:", self._value)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _validate(self) -> None:
        if not self._name.text().strip():
            QMessageBox.warning(self, "Invalid", "The name must not be empty.")
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        return self._name.text().strip(), self._value.text().strip()


class SettingsDialog(QDialog):
    """Left-nav settings dialog. Instantiate fresh per open (snapshot edits)."""

    def __init__(
        self,
        settings: Settings,
        config_dir: Path,
        *,
        twitch_status: str = "",
        youtube_status: str = "",
        youtube_api_key: str = "",
        on_youtube_api_key: Callable[[str], None] | None = None,
        connect_twitch: Callable[[], None] | None = None,
        disconnect_twitch: Callable[[], None] | None = None,
        connect_youtube: Callable[[], None] | None = None,
        disconnect_youtube: Callable[[], None] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(760, 520)
        self._settings = settings
        self._config_dir = config_dir
        self._connect_twitch = connect_twitch
        self._disconnect_twitch = disconnect_twitch
        self._connect_youtube = connect_youtube
        self._disconnect_youtube = disconnect_youtube
        # The YouTube API key lives in the keyring (SecretsStore), not
        # settings.json — MainWindow hands it in and receives it back.
        # Twitch needs no credentials: sign-in is the PKCE browser flow with
        # ChatterSyn's registered client ID.
        self._on_youtube_api_key = on_youtube_api_key
        # In-memory edit snapshots (written on accept). User commands live in
        # commands.json (Chatterino format), everything else in settings.
        self._commands: list[UserCommand] = load_user_commands(config_dir)
        self._highlights: list[dict] = [
            dict(entry) for entry in (settings.get("highlights") or [])
            if isinstance(entry, dict)
        ]
        self._moderation_actions: list[dict] = [
            dict(entry) for entry in (settings.get("moderation_actions") or [])
            if isinstance(entry, dict)
        ]
        # Absent key -> default binding; an explicitly cleared "" stays empty.
        self._hotkeys: dict[str, str] = {}
        for name, default in HOTKEY_DEFAULTS.items():
            value = settings.get(f"hotkeys.{name}")
            self._hotkeys[name] = default if value is None else str(value)

        outer = QHBoxLayout(self)
        self._nav = QListWidget(self)
        self._nav.setFixedWidth(150)
        self._nav.setStyleSheet(_NAV_STYLE)
        self._stack = QStackedWidget(self)
        outer.addWidget(self._nav)
        outer.addWidget(self._stack, 1)

        self._add_page("General", self._build_general())
        self._add_page("Accounts", self._build_accounts(
            twitch_status, youtube_status, youtube_api_key))
        self._add_page("Commands", self._build_commands())
        self._add_page("Emotes", self._build_emotes())
        self._add_page("Highlights", self._build_highlights())
        self._add_page("Moderation", self._build_moderation())
        self._add_page("Notifications", self._build_notifications())
        self._add_page("Hotkeys", self._build_hotkeys())
        self._add_page("About", self._build_about())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons, 0, Qt.AlignmentFlag.AlignBottom)
        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)

    # -- navigation ------------------------------------------------------------------

    def _add_page(self, title: str, widget: QWidget) -> None:
        QListWidgetItem(title, self._nav)
        self._stack.addWidget(widget)

    def set_twitch_status(self, text: str) -> None:
        self._twitch_status_label.setText(text)

    def set_youtube_status(self, text: str) -> None:
        self._youtube_status_label.setText(text)

    # -- pages -----------------------------------------------------------------------

    def _build_general(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        self._theme = QComboBox(page)
        self._theme.addItems(["dark", "light"])
        self._theme.setCurrentText(self._settings.get("theme", "dark"))
        form.addRow("Theme:", self._theme)

        self._scale = QDoubleSpinBox(page)
        self._scale.setRange(0.8, 1.5)
        self._scale.setSingleStep(0.05)
        self._scale.setValue(float(self._settings.get("ui_scale", 1.0)))
        form.addRow("UI scale:", self._scale)

        self._scrollback = QSpinBox(page)
        self._scrollback.setRange(100, 100000)
        self._scrollback.setValue(int(self._settings.get("scrollback", 1000)))
        form.addRow("Scrollback (rows):", self._scrollback)

        self._timestamps = QCheckBox("Show timestamps", page)
        self._timestamps.setChecked(bool(self._settings.get("timestamps.enabled", True)))
        form.addRow("", self._timestamps)

        self._timestamp_format = QLineEdit(
            self._settings.get("timestamps.format", "HH:mm:ss"), page)
        form.addRow("Timestamp format:", self._timestamp_format)

        self._badges = QCheckBox("Show Twitch chat badges", page)
        self._badges.setChecked(bool(self._settings.get("badges.enabled", True)))
        form.addRow("", self._badges)

        self._restore_tabs = QCheckBox("Restore tabs from the last session", page)
        self._restore_tabs.setChecked(
            bool(self._settings.get("general.restore_tabs", True)))
        form.addRow("", self._restore_tabs)
        return page

    def _build_accounts(self, twitch_status: str, youtube_status: str,
                        youtube_api_key: str = "") -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("<b>Twitch</b>", page))
        twitch_form = QFormLayout()
        self._twitch_status_label = QLabel(twitch_status or "Not connected", page)
        twitch_form.addRow("Status:", self._twitch_status_label)
        row = QHBoxLayout()
        connect = QPushButton("Connect Account…", page)
        connect.clicked.connect(self._on_connect_twitch)
        disconnect = QPushButton("Disconnect", page)
        disconnect.clicked.connect(self._on_disconnect_twitch)
        row.addWidget(connect)
        row.addWidget(disconnect)
        row.addStretch(1)
        twitch_form.addRow("", row)
        layout.addLayout(twitch_form)

        layout.addSpacing(12)
        layout.addWidget(QLabel("<b>YouTube</b>", page))
        youtube_form = QFormLayout()
        self._api_key = QLineEdit(youtube_api_key, page)
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        youtube_form.addRow("Data API key:", self._api_key)
        self._youtube_status_label = QLabel(youtube_status or "Not connected", page)
        youtube_form.addRow("Status:", self._youtube_status_label)
        row = QHBoxLayout()
        authorize = QPushButton("Authorize…", page)
        authorize.clicked.connect(self._on_connect_youtube)
        disconnect = QPushButton("Disconnect", page)
        disconnect.clicked.connect(self._on_disconnect_youtube)
        row.addWidget(authorize)
        row.addWidget(disconnect)
        row.addStretch(1)
        youtube_form.addRow("", row)
        layout.addLayout(youtube_form)
        note = QLabel(
            "Note: Google projects in Testing mode expire refresh tokens after "
            "7 days — you will be asked to authorize again.", page)
        note.setWordWrap(True)
        note.setStyleSheet("color: #8a8a8a; font-size: 9pt;")
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _build_commands(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self._commands_table = QTableWidget(0, 3, page)
        self._commands_table.setHorizontalHeaderLabels(["Trigger", "Output", "In context menu"])
        self._commands_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._commands_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._commands_table)
        layout.addLayout(self._table_buttons(
            self._commands_table, self._add_command, self._edit_command))
        self._reload_commands_table()
        return page

    def _reload_commands_table(self) -> None:
        table = self._commands_table
        table.setRowCount(len(self._commands))
        for row, command in enumerate(self._commands):
            table.setItem(row, 0, QTableWidgetItem(command.name))
            table.setItem(row, 1, QTableWidgetItem(command.func))
            shown = QTableWidgetItem()
            shown.setFlags(shown.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            shown.setCheckState(
                Qt.CheckState.Checked if command.show_in_context_menu
                else Qt.CheckState.Unchecked)
            table.setItem(row, 2, shown)

    def _add_command(self) -> None:
        editor = _TextPairEditor("Add Command", "Trigger", "Output", parent=self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            name, func = editor.values()
            self._commands.append(UserCommand(name, func))
            self._reload_commands_table()

    def _edit_command(self) -> None:
        row = self._commands_table.currentRow()
        if row < 0:
            return
        command = self._commands[row]
        editor = _TextPairEditor("Edit Command", "Trigger", "Output",
                                 command.name, command.func, parent=self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            name, func = editor.values()
            self._commands[row] = UserCommand(
                name, func, show_in_context_menu=command.show_in_context_menu)
            self._reload_commands_table()

    def _build_emotes(self) -> QWidget:
        """YouTube-side emotes (YouTube chat is plain text) + the image cache."""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        explain = QLabel(
            "YouTube chat can't show Twitch, 7TV, BTTV or FFZ emotes, so emotes in "
            "your messages are sent to Twitch only and left out of the YouTube "
            "copy. A message that's only emotes isn't sent to YouTube at all.", page)
        explain.setWordWrap(True)
        layout.addWidget(explain)
        self._yt_emoji = QCheckBox("On YouTube, replace emotes with a matching emoji", page)
        self._yt_emoji.setToolTip(
            "Uses the table below, e.g. KEKW → 🤣. Emotes without an emoji are "
            "still left out. Twitch always gets the real emote.")
        self._yt_emoji.setChecked(bool(self._settings.get("youtube_emotes.emoji", False)))
        layout.addWidget(self._yt_emoji)

        table_label = QLabel("Emote → emoji (names are case-sensitive):", page)
        layout.addWidget(table_label)
        self._emoji_table = QTableWidget(0, 2, page)
        self._emoji_table.setHorizontalHeaderLabels(["Emote", "Emoji"])
        self._emoji_table.horizontalHeader().setStretchLastSection(True)
        self._emoji_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        for name, emoji in sorted(emoji_map(self._settings).items(), key=lambda kv: kv[0].lower()):
            self._append_emoji_row(name, emoji)
        self._emoji_table.setColumnWidth(0, 200)  # fits FeelsGoodMan, ResidentSleeper…
        layout.addWidget(self._emoji_table, 1)
        # Takes the table's space while it's hidden, so the page stays packed
        # at the top instead of spreading out.
        self._emoji_filler = QWidget(page)
        self._emoji_filler.setSizePolicy(QSizePolicy.Policy.Expanding,
                                         QSizePolicy.Policy.Expanding)
        layout.addWidget(self._emoji_filler, 1)

        row = QHBoxLayout()
        add = QPushButton("Add", page)
        add.clicked.connect(self._add_emoji_row)
        remove = QPushButton("Remove", page)
        remove.clicked.connect(self._remove_emoji_row)
        reset = QPushButton("Reset to defaults", page)
        reset.clicked.connect(self._reset_emoji_rows)
        for button in (add, remove, reset):
            row.addWidget(button)
        # The table only matters while emoji sending is on: hidden otherwise.
        self._emoji_widgets = (table_label, self._emoji_table, add, remove, reset)
        self._yt_emoji.toggled.connect(self._sync_emoji_enabled)
        self._sync_emoji_enabled(self._yt_emoji.isChecked())
        row.addStretch(1)
        self._clear_cache = QPushButton("Clear emote cache", page)
        self._clear_cache.setToolTip("Delete downloaded emote/badge images from disk")
        self._clear_cache.setEnabled(image_cache.disk is not None)
        self._clear_cache.clicked.connect(self._clear_image_cache)
        row.addWidget(self._clear_cache)
        layout.addLayout(row)
        return page

    def _sync_emoji_enabled(self, enabled: bool) -> None:
        for widget in self._emoji_widgets:
            widget.setVisible(enabled)
        self._emoji_filler.setVisible(not enabled)

    def _append_emoji_row(self, name: str = "", emoji: str = "") -> int:
        table = self._emoji_table
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(name))
        table.setItem(row, 1, QTableWidgetItem(emoji))
        return row

    def _add_emoji_row(self) -> None:
        row = self._append_emoji_row()
        self._emoji_table.setCurrentCell(row, 0)
        self._emoji_table.editItem(self._emoji_table.item(row, 0))

    def _remove_emoji_row(self) -> None:
        row = self._emoji_table.currentRow()
        if row >= 0:
            self._emoji_table.removeRow(row)

    def _reset_emoji_rows(self) -> None:
        self._emoji_table.setRowCount(0)
        for name, emoji in sorted(DEFAULT_EMOJI.items(), key=lambda kv: kv[0].lower()):
            self._append_emoji_row(name, emoji)

    def _emoji_rows(self) -> dict[str, str]:
        """Non-empty table rows (the saved table replaces the defaults)."""
        table = self._emoji_table
        rows: dict[str, str] = {}
        for row in range(table.rowCount()):
            name = (table.item(row, 0).text() if table.item(row, 0) else "").strip()
            emoji = (table.item(row, 1).text() if table.item(row, 1) else "").strip()
            if name and emoji:
                rows[name] = emoji
        return rows

    def _clear_image_cache(self) -> None:
        if image_cache.disk is None:
            return
        try:
            image_cache.disk.clear()
        except OSError as exc:
            QMessageBox.warning(self, "Clear emote cache", f"Couldn't clear the cache: {exc}")
            return
        self._clear_cache.setText("Cache cleared")
        self._clear_cache.setEnabled(False)

    def _build_highlights(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self._highlights_table = QTableWidget(0, 5, page)
        self._highlights_table.setHorizontalHeaderLabels(
            ["Text", "Regex", "Case", "Color", "Mentions"])
        self._highlights_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self._highlights_table)
        layout.addLayout(self._table_buttons(
            self._highlights_table, self._add_highlight, self._edit_highlight))
        test_row = QHBoxLayout()
        test_row.addWidget(QLabel("Test:", page))
        self._highlight_test = QLineEdit(page)
        self._highlight_test.setPlaceholderText("Type a sample message…")
        self._highlight_test.textChanged.connect(self._preview_highlight)
        test_row.addWidget(self._highlight_test, 1)
        self._highlight_preview = QLabel("", page)
        test_row.addWidget(self._highlight_preview)
        layout.addLayout(test_row)
        self._reload_highlights_table()
        return page

    def _reload_highlights_table(self) -> None:
        table = self._highlights_table
        table.setRowCount(len(self._highlights))
        for row, entry in enumerate(self._highlights):
            table.setItem(row, 0, QTableWidgetItem(str(entry.get("text", ""))))
            table.setItem(row, 1, QTableWidgetItem("yes" if entry.get("regex") else "no"))
            table.setItem(row, 2, QTableWidgetItem("yes" if entry.get("case_sensitive") else "no"))
            table.setItem(row, 3, QTableWidgetItem(str(entry.get("color", ""))))
            table.setItem(row, 4, QTableWidgetItem("yes" if entry.get("alert") else "no"))

    def _add_highlight(self) -> None:
        editor = _HighlightEditor(None, self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            self._highlights.append(editor.entry())
            self._reload_highlights_table()
            self._preview_highlight()

    def _edit_highlight(self) -> None:
        row = self._highlights_table.currentRow()
        if row < 0:
            return
        editor = _HighlightEditor(self._highlights[row], self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            self._highlights[row] = editor.entry()
            self._reload_highlights_table()
            self._preview_highlight()

    def _preview_highlight(self) -> None:
        text = self._highlight_test.text()
        if not text.strip() or not self._highlights:
            self._highlight_preview.setText("")
            return
        matcher = HighlightMatcher(Settings({"highlights": self._highlights}))
        hit = matcher.match(
            Message.text(Platform.TWITCH, "test", Author("tester"), text))
        if hit is None:
            self._highlight_preview.setText("no match")
            self._highlight_preview.setStyleSheet("color: #8a8a8a;")
        else:
            self._highlight_preview.setText(f"matches ({hit.color})")
            self._highlight_preview.setStyleSheet(
                f"color: {hit.color}; font-weight: bold;")

    def _build_moderation(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self._moderation_table = QTableWidget(0, 2, page)
        self._moderation_table.setHorizontalHeaderLabels(["Button", "Command template"])
        self._moderation_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self._moderation_table)
        layout.addLayout(self._table_buttons(
            self._moderation_table, self._add_moderation_action,
            self._edit_moderation_action))
        hint = QLabel(
            "These appear as buttons on a user's card (click their name) when "
            "you can moderate the channel. Placeholders: {user.name}, "
            "{user.login}, {channel.name}, {msg.id}, {msg.text}. Actions using "
            "{msg.*} only show when the card was opened from that user's "
            "message.", page)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._reload_moderation_table()
        return page

    def _reload_moderation_table(self) -> None:
        table = self._moderation_table
        table.setRowCount(len(self._moderation_actions))
        for row, entry in enumerate(self._moderation_actions):
            table.setItem(row, 0, QTableWidgetItem(str(entry.get("name", ""))))
            table.setItem(row, 1, QTableWidgetItem(str(entry.get("template", ""))))

    def _add_moderation_action(self) -> None:
        editor = _TextPairEditor("Add Moderation Action", "Button label",
                                 "Command template", parent=self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            name, template = editor.values()
            self._moderation_actions.append({"name": name, "template": template})
            self._reload_moderation_table()

    def _edit_moderation_action(self) -> None:
        row = self._moderation_table.currentRow()
        if row < 0:
            return
        entry = self._moderation_actions[row]
        editor = _TextPairEditor("Edit Moderation Action", "Button label",
                                 "Command template", entry.get("name", ""),
                                 entry.get("template", ""), parent=self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            name, template = editor.values()
            self._moderation_actions[row] = {"name": name, "template": template}
            self._reload_moderation_table()

    def _table_buttons(self, table: QTableWidget, on_add, on_edit) -> QHBoxLayout:
        row = QHBoxLayout()
        add = QPushButton("Add…", self)
        add.clicked.connect(on_add)
        edit = QPushButton("Edit…", self)
        edit.clicked.connect(on_edit)
        remove = QPushButton("Remove", self)
        remove.clicked.connect(
            lambda: self._remove_selected(table))
        row.addWidget(add)
        row.addWidget(edit)
        row.addWidget(remove)
        row.addStretch(1)
        return row

    def _remove_selected(self, table: QTableWidget) -> None:
        row = table.currentRow()
        if row < 0:
            return
        if table is self._commands_table:
            self._commands.pop(row)
            self._reload_commands_table()
        elif table is self._highlights_table:
            self._highlights.pop(row)
            self._reload_highlights_table()
        elif table is self._moderation_table:
            self._moderation_actions.pop(row)
            self._reload_moderation_table()

    def _build_notifications(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        self._notify_mentions = QCheckBox("Highlight mentions", page)
        self._notify_mentions.setChecked(
            bool(self._settings.get("notifications.mentions", True)))
        self._notify_sound = QCheckBox("Play a sound", page)
        self._notify_sound.setChecked(
            bool(self._settings.get("notifications.sound", False)))
        self._notify_live = QCheckBox("Notify when a followed stream goes live",
                                      page)
        self._notify_live.setChecked(
            bool(self._settings.get("notifications.live", False)))
        form.addRow("", self._notify_mentions)
        form.addRow("", self._notify_sound)
        form.addRow("", self._notify_live)
        return page

    def _build_hotkeys(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self._hotkeys_table = QTableWidget(0, 2, page)
        self._hotkeys_table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        self._hotkeys_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self._hotkeys_table)
        row = QHBoxLayout()
        set_button = QPushButton("Set Shortcut…", page)
        set_button.clicked.connect(self._set_hotkey)
        clear_button = QPushButton("Clear", page)
        clear_button.clicked.connect(self._clear_hotkey)
        row.addWidget(set_button)
        row.addWidget(clear_button)
        row.addStretch(1)
        layout.addLayout(row)
        self._reload_hotkeys_table()
        return page

    def _reload_hotkeys_table(self) -> None:
        table = self._hotkeys_table
        table.setRowCount(len(HOTKEY_DEFAULTS))
        for row, (name, _default) in enumerate(HOTKEY_DEFAULTS.items()):
            table.setItem(row, 0, QTableWidgetItem(HOTKEY_LABELS[name]))
            table.setItem(row, 1, QTableWidgetItem(self._hotkeys[name]))

    def _selected_hotkey_name(self) -> str | None:
        row = self._hotkeys_table.currentRow()
        if row < 0:
            return None
        return list(HOTKEY_DEFAULTS)[row]

    def _set_hotkey(self) -> None:
        name = self._selected_hotkey_name()
        if name is None:
            return
        capture = _KeyCaptureDialog(self._hotkeys[name], self)
        if capture.exec() == QDialog.DialogCode.Accepted:
            sequence = capture.sequence()
            # One shortcut per action: clear it from any other action.
            for other, value in self._hotkeys.items():
                if other != name and value and value == sequence:
                    self._hotkeys[other] = ""
            self._hotkeys[name] = sequence
            self._reload_hotkeys_table()

    def _clear_hotkey(self) -> None:
        name = self._selected_hotkey_name()
        if name is None:
            return
        self._hotkeys[name] = ""
        self._reload_hotkeys_table()

    def _build_about(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel(
            f"<h2>ChatterSyn {VERSION}</h2>"
            "A Chatterino-style chat client for Twitch and YouTube live chat "
            "in one merged feed.", page))
        layout.addWidget(QLabel(
            "Built with Python, PySide6 and qasync. Settings live in "
            "settings.json and commands.json next to the app data directory.",
            page))
        layout.addStretch(1)
        return page

    # -- account callbacks ------------------------------------------------------------

    def _on_connect_twitch(self) -> None:
        if self._connect_twitch is not None:
            self._connect_twitch()
            self.set_twitch_status("Sign-in started — complete it in your browser…")

    def _on_disconnect_twitch(self) -> None:
        if self._disconnect_twitch is not None:
            self._disconnect_twitch()
            self.set_twitch_status("Not connected")

    def _on_connect_youtube(self) -> None:
        if self._connect_youtube is not None:
            self._connect_youtube()
            self.set_youtube_status("Authorization started — complete it in your browser…")

    def _on_disconnect_youtube(self) -> None:
        if self._disconnect_youtube is not None:
            self._disconnect_youtube()
            self.set_youtube_status("Not connected")

    # -- apply ------------------------------------------------------------------------

    def _commands_snapshot(self) -> list[UserCommand]:
        table = self._commands_table
        commands: list[UserCommand] = []
        for row in range(table.rowCount()):
            name = (table.item(row, 0).text() if table.item(row, 0) else "").strip()
            func = table.item(row, 1).text() if table.item(row, 1) else ""
            if not name:
                continue
            item = table.item(row, 2)
            shown = item.checkState() == Qt.CheckState.Checked if item else False
            commands.append(UserCommand(name, func, show_in_context_menu=shown))
        return commands

    def accept(self) -> None:  # noqa: D102 (Qt override)
        self._settings.set_batch({
            "theme": self._theme.currentText(),
            "ui_scale": round(self._scale.value(), 2),
            "scrollback": self._scrollback.value(),
            "timestamps.enabled": self._timestamps.isChecked(),
            "timestamps.format": self._timestamp_format.text().strip() or "HH:mm:ss",
            "badges.enabled": self._badges.isChecked(),
            "youtube_emotes.emoji": self._yt_emoji.isChecked(),
            # None = follow DEFAULT_EMOJI (picks up future additions); only
            # an actually edited table is stored.
            "youtube_emotes.map": (None if self._emoji_rows() == DEFAULT_EMOJI
                                   else self._emoji_rows()),
            "general.restore_tabs": self._restore_tabs.isChecked(),
            "highlights": self._highlights,
            "moderation_actions": self._moderation_actions,
            "notifications.mentions": self._notify_mentions.isChecked(),
            "notifications.sound": self._notify_sound.isChecked(),
            "notifications.live": self._notify_live.isChecked(),
            "hotkeys": self._hotkeys,
        })
        if self._on_youtube_api_key is not None:
            self._on_youtube_api_key(self._api_key.text().strip())
        save_user_commands(self._config_dir, self._commands_snapshot())
        super().accept()
