"""Settings dialog: pages, snapshots, apply-on-accept persistence, key capture."""

import json

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QDialog, QLineEdit, QTableWidgetItem

from chattersyn.config.settings import Settings
from chattersyn.ui.settings_dialog import (
    HOTKEY_DEFAULTS,
    SettingsDialog,
    _KeyCaptureDialog,
    _TextPairEditor,
)


@pytest.fixture
def settings_path(tmp_path):
    return tmp_path / "settings.json"


def _dialog(settings, tmp_path):
    return SettingsDialog(settings, tmp_path)


# -- structure ------------------------------------------------------------------------


def test_dialog_builds_all_pages(qapp, tmp_path):
    dialog = _dialog(Settings({}), tmp_path)
    assert dialog._nav.count() == 8
    assert dialog._stack.count() == 8
    assert dialog._nav.item(0).text() == "General"
    assert dialog._nav.item(7).text() == "About"


def test_nav_row_switches_stack_page(qapp, tmp_path):
    dialog = _dialog(Settings({}), tmp_path)
    dialog._nav.setCurrentRow(2)
    assert dialog._stack.currentIndex() == 2
    dialog._nav.setCurrentRow(0)
    assert dialog._stack.currentIndex() == 0


# -- hotkey snapshot semantics ---------------------------------------------------------


def test_hotkeys_absent_defaults_cleared_stays_cleared(qapp, tmp_path):
    settings = Settings(
        {"hotkeys": {"open_settings": "", "new_tab": "Ctrl+K"}})
    dialog = _dialog(settings, tmp_path)
    assert dialog._hotkeys["open_settings"] == ""       # cleared stays cleared
    assert dialog._hotkeys["new_tab"] == "Ctrl+K"       # custom binding
    assert dialog._hotkeys["close_tab"] == HOTKEY_DEFAULTS["close_tab"]  # default
    assert dialog._hotkeys["toggle_theme"] == ""        # default is empty


# -- key capture ------------------------------------------------------------------------


def _press(dialog, key, modifiers=Qt.KeyboardModifier.NoModifier):
    event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
    dialog.keyPressEvent(event)


def test_key_capture_records_combination(qapp):
    capture = _KeyCaptureDialog("")
    _press(capture, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
    assert capture.sequence() == "Ctrl+K"


def test_key_capture_ignores_bare_modifiers(qapp):
    capture = _KeyCaptureDialog("")
    _press(capture, Qt.Key.Key_Control)
    assert not capture._done
    assert capture.sequence() == ""


def test_key_capture_backspace_clears(qapp):
    capture = _KeyCaptureDialog("Ctrl+K")
    _press(capture, Qt.Key.Key_Backspace)
    assert capture.sequence() == ""
    assert capture._done  # cleared counts as a chosen result


def test_key_capture_escape_rejects(qapp):
    capture = _KeyCaptureDialog("")
    _press(capture, Qt.Key.Key_Escape)
    assert capture.result() == QDialog.DialogCode.Rejected  # rejected, not accepted


# -- text pair editor -------------------------------------------------------------------


def test_text_pair_editor_values(qapp):
    editor = _TextPairEditor("Add", "Trigger", "Output")
    editor._name.setText("hello")
    editor._value.setText("HeyGuys {1+}")
    assert editor.values() == ("hello", "HeyGuys {1+}")


# -- commands.json roundtrip --------------------------------------------------------------


def test_commands_load_from_commands_json_and_save_edits(qapp, tmp_path, settings_path):
    (tmp_path / "commands.json").write_text(json.dumps({
        "commands": [
            {"name": "hello", "func": "HeyGuys {1+}", "showInMsgContextMenu": True},
        ],
    }), encoding="utf-8")
    dialog = _dialog(Settings({}, settings_path), tmp_path)
    assert dialog._commands_table.rowCount() == 1
    assert dialog._commands_table.item(0, 0).text() == "hello"
    # Edit the func in place, uncheck the context-menu flag, and accept.
    dialog._commands_table.setItem(0, 1, QTableWidgetItem("New text {1}"))
    dialog._commands_table.item(0, 2).setCheckState(Qt.CheckState.Unchecked)
    dialog.accept()
    data = json.loads((tmp_path / "commands.json").read_text(encoding="utf-8"))
    assert data["commands"] == [
        {"name": "hello", "func": "New text {1}", "showInMsgContextMenu": False}]


def test_commands_snapshot_skips_blank_rows(qapp, tmp_path, settings_path):
    dialog = _dialog(Settings({}, settings_path), tmp_path)
    dialog._commands_table.setRowCount(2)
    dialog._commands_table.setItem(0, 0, QTableWidgetItem("ok"))
    dialog._commands_table.setItem(0, 1, QTableWidgetItem("fine"))
    dialog._commands_table.setItem(1, 1, QTableWidgetItem("no name"))
    dialog.accept()
    data = json.loads((tmp_path / "commands.json").read_text(encoding="utf-8"))
    assert data["commands"] == [{"name": "ok", "func": "fine",
                                 "showInMsgContextMenu": False}]


# -- accept persistence ------------------------------------------------------------------


def test_accept_writes_general_and_accounts(qapp, tmp_path, settings_path):
    captured: list[str] = []
    api_keys: list[str] = []
    dialog = SettingsDialog(Settings({}, settings_path), tmp_path,
                            twitch_client_secret="old",
                            on_twitch_secret=captured.append,
                            youtube_api_key="stored-key",
                            on_youtube_api_key=api_keys.append)
    dialog._theme.setCurrentText("light")
    dialog._scale.setValue(1.25)
    dialog._scrollback.setValue(500)
    dialog._restore_tabs.setChecked(False)
    dialog._client_id.setText("abc123")
    dialog._client_secret.setText("s3cret")
    dialog._api_key.setText("new-key")
    dialog.accept()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["theme"] == "light"
    assert data["ui_scale"] == 1.25
    assert data["scrollback"] == 500
    assert data["general"]["restore_tabs"] is False
    assert data["twitch"]["client_id"] == "abc123"
    # The client secret and the API key never land in settings.json — both
    # go to the keyring via callbacks (SecretsStore in MainWindow).
    assert "client_secret" not in data["twitch"]
    assert captured == ["s3cret"]
    assert "api_key" not in data.get("youtube", {})
    assert api_keys == ["new-key"]


def test_api_key_field_is_masked_and_prefilled(qapp, tmp_path, settings_path):
    dialog = SettingsDialog(Settings({}, settings_path), tmp_path,
                            youtube_api_key="stored-key")
    assert dialog._api_key.text() == "stored-key"
    assert dialog._api_key.echoMode() == QLineEdit.EchoMode.Password


def test_accept_cleared_api_key_reaches_callback_as_empty(qapp, tmp_path, settings_path):
    captured: list[str] = []
    dialog = SettingsDialog(Settings({}, settings_path), tmp_path,
                            youtube_api_key="stored-key",
                            on_youtube_api_key=captured.append)
    dialog._api_key.setText("")
    dialog.accept()
    assert captured == [""]  # MainWindow deletes the keyring entry


def test_accept_cleared_secret_reaches_callback_as_empty(qapp, tmp_path, settings_path):
    captured: list[str] = []
    dialog = SettingsDialog(Settings({}, settings_path), tmp_path,
                            twitch_client_secret="stale",
                            on_twitch_secret=captured.append)
    dialog._client_secret.setText("")
    dialog.accept()
    assert captured == [""]  # MainWindow deletes the keyring entry


def test_accept_roundtrips_highlights_and_moderation_actions(
        qapp, tmp_path, settings_path):
    settings = Settings({
        "highlights": [
            {"text": "kappa", "regex": False, "case_sensitive": False,
             "color": "#ff0000", "alert": True, "sound": False}],
        "moderation_actions": [{"name": "Ban", "template": "/ban {user.name}"}],
    }, settings_path)
    dialog = _dialog(settings, tmp_path)
    dialog.accept()  # untouched -> written back verbatim
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["highlights"] == settings.get("highlights")
    assert data["moderation_actions"] == settings.get("moderation_actions")


def test_accept_saves_hotkey_rebind(qapp, tmp_path, settings_path):
    dialog = _dialog(Settings({}, settings_path), tmp_path)
    dialog._hotkeys["new_tab"] = "Ctrl+N"
    dialog.accept()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["hotkeys"]["new_tab"] == "Ctrl+N"


# -- highlight test preview ---------------------------------------------------------------


def _highlight_dialog(tmp_path, settings_path):
    return _dialog(Settings({
        "highlights": [
            {"text": "kappa", "regex": False, "case_sensitive": False,
             "color": "#ff0000", "alert": True, "sound": False}],
    }, settings_path), tmp_path)


def test_highlight_preview_match_and_miss(qapp, tmp_path, settings_path):
    dialog = _highlight_dialog(tmp_path, settings_path)
    dialog._highlight_test.setText("Kappa is here")
    assert "matches" in dialog._highlight_preview.text()
    dialog._highlight_test.setText("nothing to see")
    assert dialog._highlight_preview.text() == "no match"


def test_highlight_preview_blank_text_is_empty(qapp, tmp_path, settings_path):
    dialog = _highlight_dialog(tmp_path, settings_path)
    dialog._highlight_test.setText("   ")
    assert dialog._highlight_preview.text() == ""


# -- highlight table editing ---------------------------------------------------------------


def test_highlight_remove_selected(qapp, tmp_path, settings_path):
    dialog = _highlight_dialog(tmp_path, settings_path)
    assert dialog._highlights_table.rowCount() == 1
    dialog._highlights_table.setCurrentCell(0, 0)
    dialog._remove_selected(dialog._highlights_table)
    assert dialog._highlights == []
    assert dialog._highlights_table.rowCount() == 0


def test_highlight_add_roundtrip(qapp, tmp_path, settings_path):
    dialog = _highlight_dialog(tmp_path, settings_path)
    dialog._highlights.append({
        "text": "new", "regex": True, "case_sensitive": True,
        "color": "#00ff00", "alert": False, "sound": True})
    dialog._reload_highlights_table()
    assert dialog._highlights_table.rowCount() == 2
    dialog.accept()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert len(data["highlights"]) == 2


# -- status labels / account callbacks ------------------------------------------------------


def test_account_status_labels_and_callbacks(qapp, tmp_path):
    called = []
    dialog = SettingsDialog(Settings({}), tmp_path,
                            connect_twitch=lambda: called.append("tw"),
                            disconnect_youtube=lambda: called.append("yt-off"))
    dialog.set_twitch_status("Connected as forsen")
    assert dialog._twitch_status_label.text() == "Connected as forsen"
    dialog._on_connect_twitch()
    assert called == ["tw"]
    assert "Sign-in started" in dialog._twitch_status_label.text()
    dialog._on_disconnect_youtube()
    assert called == ["tw", "yt-off"]
    assert dialog._youtube_status_label.text() == "Not connected"
