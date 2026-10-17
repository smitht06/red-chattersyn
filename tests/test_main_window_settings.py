"""MainWindow settings wiring: hotkey registry, geometry persistence,
restore-tabs gate, apply-on-accept plumbing, account disconnect helpers."""

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialog, QWidget

from chattersyn.config.settings import Settings
from chattersyn.ui.main_window import MainWindow


@pytest.fixture
def qsettings(tmp_path):
    return QSettings(str(tmp_path / "qt.ini"), QSettings.Format.IniFormat)


@pytest.fixture
async def win(qapp, qsettings):
    w = MainWindow(Settings({}), demo=True, qsettings=qsettings)
    for feed in list(w._feeds):  # silence the demo feed generator
        feed.stop()
    w._feeds.clear()
    yield w
    w.close()


# -- hotkey registry -----------------------------------------------------------------


async def test_hotkeys_registered_from_defaults(win):
    assert "new_tab" in win._hotkey_shortcuts
    assert "close_tab" in win._hotkey_shortcuts
    assert win._hotkey_shortcuts["new_tab"].key().toString() == "Ctrl+T"
    assert "toggle_theme" not in win._hotkey_shortcuts  # default binding is empty


async def test_hotkeys_respect_custom_bindings_and_empty(qapp, qsettings, tmp_path):
    # Overrides deep-merge with the defaults (Settings semantics): custom
    # bindings win for the edited keys, the rest stay default.
    w = MainWindow(Settings({"hotkeys": {"new_tab": "Ctrl+N", "toggle_theme": "F8"}}),
                   demo=True, qsettings=qsettings)
    try:
        assert w._hotkey_shortcuts["new_tab"].key().toString() == "Ctrl+N"
        assert w._hotkey_shortcuts["toggle_theme"].key().toString() == "F8"
        assert w._hotkey_shortcuts["open_settings"].key().toString() == "Ctrl+,"
    finally:
        w.close()


async def test_rebind_hotkeys_after_settings_change(win):
    win._settings.set("hotkeys.new_tab", "Ctrl+Shift+N")
    win._rebind_hotkeys()
    assert win._hotkey_shortcuts["new_tab"].key().toString() == "Ctrl+Shift+N"
    win._settings.set("hotkeys.new_tab", "")
    win._rebind_hotkeys()
    assert "new_tab" not in win._hotkey_shortcuts


async def test_hotkey_handlers_cover_all_actions(win):
    assert set(win._hotkey_handlers()) == {
        "open_settings", "new_tab", "close_tab", "next_tab", "prev_tab",
        "focus_input", "toggle_search", "toggle_theme", "emote_picker"}


# -- tab cycling / focus / search ------------------------------------------------------


async def test_notebook_next_prev_wrap(win):
    win._notebook.addTab(QWidget(), "extra")
    assert win._notebook.currentIndex() == 0
    win._notebook_next()
    assert win._notebook.currentIndex() == 1
    win._notebook_next()
    assert win._notebook.currentIndex() == 0
    win._notebook_prev()
    assert win._notebook.currentIndex() == 1


async def test_toggle_search_opens_popup_for_current_tab(win):
    win._toggle_search()
    assert win._search_popup is not None
    win._search_popup.close()


async def test_focus_input_no_crash(win):
    win._focus_input()  # window never shown — just must not raise


# -- geometry persistence ---------------------------------------------------------------


async def test_close_saves_geometry_and_second_window_restores(qapp, qsettings):
    w1 = MainWindow(Settings({}), demo=True, qsettings=qsettings)
    w1.resize(912, 608)
    assert w1._qsettings.value("main_window/geometry") is None  # not saved yet
    w1.close()
    assert w1._qsettings.value("main_window/geometry") is not None

    w2 = MainWindow(Settings({}), demo=True, qsettings=qsettings)
    try:
        assert (w2.width(), w2.height()) == (912, 608)
    finally:
        w2.close()


# -- restore-tabs gate -------------------------------------------------------------------


async def test_saved_tabs_restored_by_default(qapp, qsettings):
    w = MainWindow(Settings({"tabs": [{"twitch_channel": "xqc"}]}),
                   demo=True, qsettings=qsettings)
    try:
        assert [tab.twitch_channel for tab in w._tabs] == ["xqc"]
    finally:
        w.close()


async def test_saved_tabs_skipped_when_restore_disabled(qapp, qsettings):
    w = MainWindow(Settings({"tabs": [{"twitch_channel": "xqc"}],
                            "general": {"restore_tabs": False}}),
                   demo=True, qsettings=qsettings)
    try:
        assert [tab.twitch_channel for tab in w._tabs] == ["forsen"]  # default tab
    finally:
        w.close()


# -- apply settings -----------------------------------------------------------------------


async def test_apply_settings_propagates_scrollback_and_scale(win):
    win._settings.set("scrollback", 500)
    win._settings.set("ui_scale", 1.2)
    win._apply_settings()
    assert win._tabs[0].view.message_model.max_messages == 500


async def test_apply_settings_updates_moderation_action_names(win):
    win._settings.set("moderation_actions", [{"name": "Ban", "template": "/ban {user.name}"}])
    win._apply_settings()
    assert win._mod_action_names() == ["Ban"]


# -- open settings dialog plumbing ---------------------------------------------------------


class _FakeSettingsDialog:
    captured: list[dict] = []

    def __init__(self, *args, **kwargs):
        _FakeSettingsDialog.captured.append(kwargs)

    def exec(self):
        return QDialog.DialogCode.Accepted


async def test_open_settings_builds_dialog_and_applies(win, monkeypatch):
    applied = []
    monkeypatch.setattr(win, "_apply_settings", lambda: applied.append(1))
    monkeypatch.setattr("chattersyn.ui.main_window.SettingsDialog", _FakeSettingsDialog)
    _FakeSettingsDialog.captured.clear()

    win._open_settings()

    assert applied == [1]
    kwargs = _FakeSettingsDialog.captured[0]
    assert kwargs["twitch_status"] == "Not connected"
    assert kwargs["youtube_status"] == "Not connected"
    assert kwargs["on_twitch_secret"] is None  # demo mode: no secrets store wiring
    assert kwargs["on_youtube_api_key"] is None
    assert kwargs["connect_twitch"] is None


class _FakeSecrets:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


async def test_save_twitch_secret_routes_to_secrets_store(win):
    win._secrets = _FakeSecrets()
    win._save_twitch_secret("s3cret")
    assert win._secrets.data == {"twitch_client_secret": "s3cret"}
    win._save_twitch_secret("")
    assert win._secrets.data == {}


async def test_save_youtube_api_key_routes_to_secrets_store(win):
    win._secrets = _FakeSecrets()
    win._save_youtube_api_key("AIza-key")
    assert win._secrets.data == {"youtube_api_key": "AIza-key"}
    win._save_youtube_api_key("")
    assert win._secrets.data == {}


async def test_disconnect_helpers_noop_in_demo(win):
    win._disconnect_twitch_account()  # no services: must not raise
    win._disconnect_youtube_account()
