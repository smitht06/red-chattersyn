"""Settings dialog: pages, snapshots, apply-on-accept persistence, key capture."""

import json

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QTableWidgetItem

from chattersyn import version
from chattersyn.config import links
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
    assert dialog._nav.count() == 10
    assert dialog._stack.count() == 10
    assert dialog._nav.item(0).text() == "General"
    assert dialog._nav.item(3).text() == "Emotes"
    assert dialog._nav.item(8).text() == "Browser Sources"
    assert dialog._nav.item(9).text() == "About"


def test_about_page_shows_the_running_version_and_required_links(qapp, tmp_path):
    page = _dialog(Settings({}), tmp_path)._stack.widget(9)
    text = " ".join(label.text() for label in page.findChildren(QLabel))
    assert f"ChatterSyn {version()}" in text  # was a stale hard-coded 0.1.0
    for url in (links.PRIVACY_POLICY, links.TERMS, links.YOUTUBE_TERMS,
                links.GOOGLE_PRIVACY, links.GOOGLE_CONNECTIONS):
        assert url in text
    assert "Open Diagnostic Log Folder" in [b.text() for b in page.findChildren(QPushButton)]


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
    api_keys: list[str] = []
    dialog = SettingsDialog(Settings({}, settings_path), tmp_path,
                            youtube_api_key="stored-key",
                            on_youtube_api_key=api_keys.append)
    dialog._theme.setCurrentText("light")
    dialog._scale.setValue(1.25)
    dialog._scrollback.setValue(500)
    dialog._restore_tabs.setChecked(False)
    assert dialog._idle_jump.isChecked()  # idle jump-to-latest default on
    dialog._idle_jump.setChecked(False)
    assert dialog._badges.isChecked()  # badges default on
    dialog._badges.setChecked(False)
    assert dialog._chat_logs.isChecked()  # chat logs default on
    dialog._chat_logs.setChecked(False)
    dialog._api_key.setText("new-key")
    dialog.accept()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["theme"] == "light"
    assert data["ui_scale"] == 1.25
    assert data["scrollback"] == 500
    assert data["general"]["restore_tabs"] is False
    assert data["general"]["jump_to_latest_when_idle"] is False
    assert data["badges"]["enabled"] is False
    assert data["logging"]["enabled"] is False
    # Twitch has no credential fields anymore: sign-in is the browser flow
    # with the baked-in client ID, so the dialog writes nothing for Twitch
    # (the file only carries the default, an advanced manual override slot).
    assert data["twitch"]["client_id"] == ""
    # The API key never lands in settings.json — it goes to the keyring via
    # the callback (SecretsStore in MainWindow).
    assert "api_key" not in data.get("youtube", {})
    assert api_keys == ["new-key"]


def test_accounts_twitch_section_has_no_credential_fields(qapp, tmp_path, settings_path):
    dialog = SettingsDialog(Settings({}, settings_path), tmp_path,
                            twitch_status="Connected as testuser")
    assert not hasattr(dialog, "_client_id")
    assert not hasattr(dialog, "_client_secret")
    assert dialog._twitch_status_label.text() == "Connected as testuser"


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


# -- Emotes page ------------------------------------------------------------------------


def test_emotes_page_defaults_and_persists_edits(qapp, tmp_path):
    from chattersyn.services.emotes.youtube_emoji import DEFAULT_EMOJI

    settings = Settings({})
    dialog = _dialog(settings, tmp_path)
    assert not dialog._yt_emoji.isChecked()  # emotes are removed by default
    assert dialog._emoji_table.isHidden()  # table only shown with emoji on
    assert dialog._emoji_rows() == DEFAULT_EMOJI
    dialog.accept()
    assert settings.get("youtube_emotes.map") is None  # untouched table: follow defaults

    dialog = _dialog(settings, tmp_path)
    dialog._yt_emoji.setChecked(True)
    assert not dialog._emoji_table.isHidden()
    dialog._emoji_table.setRowCount(0)
    row = dialog._append_emoji_row("forsenE", "🙃")
    dialog._append_emoji_row("", "ignored")  # blank names are skipped
    assert row == 0
    dialog.accept()
    assert settings.get("youtube_emotes.emoji") is True
    assert settings.get("youtube_emotes.map") == {"forsenE": "🙃"}
    dialog = _dialog(settings, tmp_path)
    assert dialog._emoji_rows() == {"forsenE": "🙃"}
    dialog._reset_emoji_rows()
    assert dialog._emoji_rows() == DEFAULT_EMOJI



def test_emotes_page_has_one_option(qapp, tmp_path):
    """One clear checkbox: the removal itself isn't optional, only whether
    YouTube gets an emoji instead."""
    from PySide6.QtWidgets import QCheckBox

    dialog = _dialog(Settings({}), tmp_path)
    page = dialog._stack.widget(3)
    assert [box.text() for box in page.findChildren(QCheckBox)] == [
        "On YouTube, replace emotes with a matching emoji"]


# -- API key note (YQB-26, YQB-43, YQB-44) -----------------------------------------------


def test_api_key_note_shown_when_key_rejected_but_signed_in(qapp, tmp_path):
    text = "API key not working — using your Google sign-in"  # YQB-26
    dialog = SettingsDialog(Settings({}), tmp_path, youtube_api_key_note=text)
    assert not dialog._api_key_note.isHidden()
    assert dialog._api_key_note.text() == text


def test_api_key_note_shown_when_key_rejected_and_signed_out(qapp, tmp_path):
    text = "API key not working — YouTube reading paused"  # YQB-43
    dialog = SettingsDialog(Settings({}), tmp_path, youtube_api_key_note=text)
    assert not dialog._api_key_note.isHidden()
    assert dialog._api_key_note.text() == text


def test_api_key_note_hidden_when_key_is_fine(qapp, tmp_path):
    # YQB-44: once the key works (or was replaced) there is no note.
    dialog = SettingsDialog(Settings({}), tmp_path, youtube_api_key_note="")
    assert dialog._api_key_note.isHidden()
    assert _dialog(Settings({}), tmp_path)._api_key_note.isHidden()



# -- Browser Sources (OBS-1, OBS-3, OBS-4) -------------------------------------------------


def test_browser_sources_defaults(qapp, tmp_path):
    """OBS-1: off by default, port 17830, the spec's default look."""
    dialog = _dialog(Settings({}), tmp_path)
    assert not dialog._bs_enabled.isChecked()
    assert dialog._bs_enabled.text() == "Show chat as a browser source for OBS"
    assert dialog._bs_port.value() == 17830
    assert dialog._bs_url.text() == "http://localhost:17830/chat"
    assert dialog._bs_size.value() == 20
    assert dialog._bs_outline.isChecked()
    assert dialog._bs_icons.isChecked() and dialog._bs_badges.isChecked()
    assert dialog._bs_emotes.isChecked() and not dialog._bs_timestamps.isChecked()
    assert dialog._bs_fade.value() == 30 and dialog._bs_limit.value() == 50
    assert dialog._bs_status.text() == "Off."


def test_browser_sources_ranges(qapp, tmp_path):
    dialog = _dialog(Settings({}), tmp_path)
    assert (dialog._bs_port.minimum(), dialog._bs_port.maximum()) == (1024, 65535)
    assert (dialog._bs_size.minimum(), dialog._bs_size.maximum()) == (10, 72)
    assert (dialog._bs_fade.minimum(), dialog._bs_fade.maximum()) == (1, 600)
    assert (dialog._bs_limit.minimum(), dialog._bs_limit.maximum()) == (5, 200)


def test_browser_sources_url_follows_port_and_copies(qapp, tmp_path):
    """OBS-3."""
    from PySide6.QtGui import QGuiApplication
    dialog = _dialog(Settings({}), tmp_path)
    dialog._bs_port.setValue(17900)
    assert dialog._bs_url.text() == "http://localhost:17900/chat"
    dialog._bs_copy.click()
    assert QGuiApplication.clipboard().text() == "http://localhost:17900/chat"


def test_browser_sources_status_passed_in(qapp, tmp_path):
    """OBS-4: the window's status text is shown."""
    dialog = SettingsDialog(Settings({}), tmp_path,
                            browser_source_status="Port 17830 is in use — choose another.")
    assert dialog._bs_status.text() == "Port 17830 is in use — choose another."


def test_browser_sources_saved_on_accept(qapp, tmp_path, settings_path):
    settings = Settings({}, settings_path)
    dialog = SettingsDialog(settings, tmp_path)
    dialog._bs_enabled.setChecked(True)
    dialog._bs_port.setValue(18000)
    dialog._bs_size.setValue(28)
    dialog._bs_text_color = "#ffcc00"
    dialog._bs_background = "#80000000"
    dialog._bs_outline.setChecked(False)
    dialog._bs_icons.setChecked(False)
    dialog._bs_badges.setChecked(False)
    dialog._bs_emotes.setChecked(False)
    dialog._bs_timestamps.setChecked(True)
    dialog._bs_keep.setChecked(True)
    dialog._bs_limit.setValue(20)
    dialog.accept()
    saved = json.loads(settings_path.read_text(encoding="utf-8"))["browser_source"]
    assert saved["enabled"] is True and saved["port"] == 18000
    assert saved["font_size"] == 28 and saved["text_color"] == "#ffcc00"
    assert saved["background"] == "#80000000" and saved["outline"] is False
    assert saved["show_platform_icons"] is False and saved["show_badges"] is False
    assert saved["show_emotes"] is False and saved["show_timestamps"] is True
    assert saved["fade_seconds"] == 0 and saved["max_messages"] == 20
    assert isinstance(saved["font_family"], str)


def test_background_fills_source_checkbox(qapp, tmp_path, settings_path):
    """User request: the background fills the whole source, or sits behind each
    message only."""
    settings = Settings({}, settings_path)
    dialog = SettingsDialog(settings, tmp_path)
    box = dialog._bs_bg_fill
    assert box.text() == "Background fills the whole source (not just behind messages)"
    assert box.isChecked()
    box.setChecked(False)
    dialog.accept()
    saved = json.loads(settings_path.read_text(encoding="utf-8"))["browser_source"]
    assert saved["background_fills_source"] is False


def test_keep_messages_checkbox(qapp, tmp_path, settings_path):
    """User request: 'never disappear until the limit is reached' as its own switch."""
    settings = Settings({}, settings_path)
    dialog = SettingsDialog(settings, tmp_path)
    assert dialog._bs_keep.text() == "Keep messages until the limit is reached"
    assert not dialog._bs_keep.isChecked() and dialog._bs_fade.isEnabled()
    dialog._bs_keep.setChecked(True)
    assert not dialog._bs_fade.isEnabled()
    dialog.accept()
    saved = json.loads(settings_path.read_text(encoding="utf-8"))["browser_source"]
    assert saved["fade_seconds"] == 0


def test_keep_messages_reflects_saved_zero_and_restores_a_time(qapp, tmp_path):
    dialog = SettingsDialog(Settings({"browser_source": {"fade_seconds": 0}}), tmp_path)
    assert dialog._bs_keep.isChecked() and not dialog._bs_fade.isEnabled()
    dialog._bs_keep.setChecked(False)
    assert dialog._bs_fade.isEnabled() and dialog._bs_fade.value() == 30


def test_untouched_font_stays_default(qapp, tmp_path, settings_path):
    """Final review #12: OK without touching the font keeps "" (the page default)."""
    settings = Settings({}, settings_path)
    SettingsDialog(settings, tmp_path).accept()
    saved = json.loads(settings_path.read_text(encoding="utf-8"))["browser_source"]
    assert saved["font_family"] == ""


def test_colour_buttons_show_the_colour(qapp, tmp_path):
    """Final review #13."""
    dialog = SettingsDialog(Settings({"browser_source": {"text_color": "#ffcc00"}}), tmp_path)
    assert "#ffcc00" in dialog._bs_text_button.styleSheet().lower()


def test_hint_mentions_starting_chattersyn_first(qapp, tmp_path):
    """Final review #4: OBS doesn't retry a page that failed to load."""
    dialog = _dialog(Settings({}), tmp_path)
    page = dialog._stack.widget(8)
    text = " ".join(label.text() for label in page.findChildren(QLabel))
    assert "Refresh" in text
