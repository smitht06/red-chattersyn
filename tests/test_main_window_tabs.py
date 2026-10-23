"""Tab titles, per-type new-tab creation (stream/twitch/youtube), the empty
notebook link page, streamer binding, and tab restore."""

import asyncio
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QInputDialog

from chattersyn.config.settings import Settings
from chattersyn.services.youtube.resolver import MINE_TARGET
from chattersyn.ui.main_window import MainWindow, _tab_title


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


# -- title composition -----------------------------------------------------------------


def test_tab_title_twitch_only():
    assert _tab_title("forsen", "") == "forsen"


def test_tab_title_youtube_only():
    assert _tab_title("", "dQw4w9WgXcQ") == "dQw4w9WgXcQ"  # short: no ellipsis
    assert _tab_title("", "watch?v=abc123def") == "watch?v=abc…"


def test_tab_title_combo_joins_both():
    assert _tab_title("forsen", "watch?v=abc123def") == "forsen + watch?v=abc…"


def test_tab_title_mine_unbound_and_bound():
    assert _tab_title("", MINE_TARGET) == "Your Stream"
    assert _tab_title("mylogin", MINE_TARGET) == "mylogin"


def test_tab_title_nothing_configured_is_empty():
    assert _tab_title("", "") == ""


# -- notebook labels -------------------------------------------------------------------


async def test_add_tab_labels_show_only_configured_chats(win):
    twitch_only = win.add_tab("forsen", None)
    youtube_only = win.add_tab("", "dQw4w9WgXcQ")
    combo = win.add_tab("xqc", "watch?v=abc123def")
    assert win._notebook.tabText(win._notebook.indexOf(twitch_only)) == "forsen"
    assert win._notebook.tabText(win._notebook.indexOf(youtube_only)) == "dQw4w9WgXcQ"
    assert win._notebook.tabText(win._notebook.indexOf(combo)) == "xqc + watch?v=abc…"


# -- new-tab dialogs --------------------------------------------------------------------


async def test_new_twitch_tab_dialog_creates_twitch_only(win, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("xqc", True))
    win._new_twitch_tab()
    tab = win._tabs[-1]
    assert tab.twitch_channel == "xqc"
    assert tab.youtube_title == ""
    assert win._notebook.tabText(win._notebook.indexOf(tab)) == "xqc"


async def test_new_twitch_tab_dialog_empty_falls_back_to_default_channel(win, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("", True))
    win._new_twitch_tab()
    assert win._tabs[-1].twitch_channel == "redcyberdragon29"


async def test_new_youtube_tab_dialog_creates_youtube_only(win, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("dQw4w9WgXcQ", True))
    win._new_youtube_tab()
    tab = win._tabs[-1]
    assert tab.twitch_channel == ""
    assert tab.youtube_title == "dQw4w9WgXcQ"
    assert win._notebook.tabText(win._notebook.indexOf(tab)) == "dQw4w9WgXcQ"


async def test_new_youtube_tab_dialog_empty_creates_nothing(win, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("", True))
    before = len(win._tabs)
    win._new_youtube_tab()
    assert len(win._tabs) == before


# -- streamer tabs -----------------------------------------------------------------------


async def test_new_streamer_tab_creates_streamer_tab(win):
    tab = win._new_streamer_tab()
    assert tab in win._streamer_tabs
    assert tab.twitch_channel == ""  # unbound until Twitch connects
    assert tab.youtube_title == "Your Stream"  # the "mine:" sentinel never leaks
    assert win._notebook.tabText(win._notebook.indexOf(tab)) == "Your Stream"


async def test_new_streamer_tab_demo_target_unset(win):
    # Demo mode never starts a watch, so the target attribute stays empty
    # (non-demo add_tab sets it in the watch branch).
    tab = win._new_streamer_tab()
    assert tab.youtube_target == ""


async def test_notebook_plus_button_creates_streamer_tab(win):
    win._notebook.new_tab_requested.emit()
    assert win._tabs[-1] in win._streamer_tabs


async def test_hotkey_new_tab_routes_to_streamer(win):
    win._hotkey_handlers()["new_tab"]()
    assert win._tabs[-1] in win._streamer_tabs


async def test_streamer_tab_binds_on_twitch_login(win):
    win._twitch = SimpleNamespace(tokens=SimpleNamespace(login="mylogin"))
    joined: list = []

    async def recorder(tab):
        joined.append(tab)

    win._join_twitch = recorder
    win._close_tab(0)  # drop the first-run default Stream tab
    win._new_streamer_tab()
    tab = win._tabs[-1]
    assert tab.twitch_channel == "mylogin"
    assert win._notebook.tabText(win._notebook.indexOf(tab)) == "mylogin"
    await asyncio.sleep(0)  # let the scheduled join run
    assert joined == [tab]


async def test_streamer_tab_binding_is_idempotent_and_sticky(win):
    win._twitch = SimpleNamespace(tokens=SimpleNamespace(login="mylogin"))
    joined: list = []

    async def recorder(tab):
        joined.append(tab)

    win._join_twitch = recorder
    win._close_tab(0)  # drop the first-run default Stream tab
    tab = win._new_streamer_tab()
    win._bind_streamer_tabs()  # re-bind pass: already bound, must be a no-op
    await asyncio.sleep(0)  # let any scheduled join run (there must be none)
    assert tab.twitch_channel == "mylogin"
    assert joined == [tab]


async def test_streamer_tab_binding_requires_tokens(win):
    win._twitch = SimpleNamespace(tokens=None)  # signed out
    tab = win._new_streamer_tab()
    assert tab.twitch_channel == ""
    win._twitch = None  # no service at all (demo)
    win._bind_streamer_tabs()  # must not crash


async def test_close_streamer_tab_removes_from_registry(win):
    tab = win._new_streamer_tab()
    win._close_tab(win._notebook.indexOf(tab))
    assert tab not in win._streamer_tabs


# -- menu --------------------------------------------------------------------------------


async def test_file_menu_offers_all_three_new_tab_types(win):
    file_menu = win.menuBar().actions()[0].menu()
    texts = [a.text() for a in file_menu.actions() if not a.isSeparator()]
    assert texts[:4] == [
        "New Stream Tab", "New Twitch Tab", "New YouTube Tab", "Close Tab"]


async def test_zoom_bindings_are_registered_hotkeys(win):
    # Zoom is in the user-editable hotkey registry (not hardcoded menu keys).
    keys = {name: s.key().toString() for name, s in win._hotkey_shortcuts.items()
            if name.startswith("zoom_")}
    assert keys == {"zoom_in": "Ctrl+=", "zoom_out": "Ctrl+-", "zoom_reset": "Ctrl+0"}


async def test_zoom_action_scales_ui(win):
    win._settings.set("ui_scale", 1.0)
    win._zoom(1.1)
    assert win._settings.get("ui_scale", 1.0) == pytest.approx(1.1)
    win._zoom(1.0, reset=True)
    assert win._settings.get("ui_scale", 1.0) == 1.0


def _wheel(angle: int, ctrl: bool) -> QWheelEvent:
    mods = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
    return QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, angle),
                       Qt.MouseButton.NoButton, mods, Qt.ScrollPhase.NoScrollPhase, False)


async def test_ctrl_wheel_zooms_in_and_out(win):
    win._settings.set("ui_scale", 1.0)
    assert win.eventFilter(win, _wheel(120, ctrl=True)) is True  # up -> zoom in
    assert win._settings.get("ui_scale", 1.0) == pytest.approx(1.1)
    assert win.eventFilter(win, _wheel(-120, ctrl=True)) is True  # down -> zoom out
    assert win._settings.get("ui_scale", 1.0) == pytest.approx(1.0, abs=1e-6)


async def test_plain_wheel_is_not_consumed(win):
    win._settings.set("ui_scale", 1.0)
    assert win.eventFilter(win, _wheel(120, ctrl=False)) is False
    assert win._settings.get("ui_scale", 1.0) == 1.0


# -- empty state -------------------------------------------------------------------------


async def test_empty_page_replaces_notebook_when_last_tab_closes(win):
    assert win._stack.currentWidget() is win._notebook
    win._close_tab(0)
    assert win._stack.currentWidget() is win._empty_tab_page
    win.add_tab("xqc", None)
    assert win._stack.currentWidget() is win._notebook


async def test_empty_page_links_create_tabs(win, monkeypatch):
    win._close_tab(0)
    assert win._stack.currentWidget() is win._empty_tab_page

    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("xqc", True))
    win._empty_tab_page.new_twitch.emit()
    assert win._tabs[-1].twitch_channel == "xqc"
    assert win._stack.currentWidget() is win._notebook

    win._close_tab(win._notebook.count() - 1)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("dQw4w9WgXcQ", True))
    win._empty_tab_page.new_youtube.emit()
    tab = win._tabs[-1]
    assert tab.twitch_channel == ""
    assert tab.youtube_title == "dQw4w9WgXcQ"


async def test_empty_page_stream_link_creates_streamer_tab(win):
    win._close_tab(0)
    assert win._stack.currentWidget() is win._empty_tab_page
    win._empty_tab_page.new_stream.emit()
    assert win._tabs[-1] in win._streamer_tabs
    assert win._stack.currentWidget() is win._notebook


# -- restore -----------------------------------------------------------------------------


async def test_restore_youtube_only_tab(qapp, qsettings):
    w = MainWindow(Settings({"tabs": [{"twitch_channel": "",
                                       "youtube": {"video_id": "abc123"}}]}),
                   demo=True, qsettings=qsettings)
    try:
        assert [tab.twitch_channel for tab in w._tabs] == [""]
        assert [tab.youtube_title for tab in w._tabs] == ["abc123"]
        assert w._notebook.tabText(0) == "abc123"
    finally:
        w.close()


async def test_first_run_default_tab_is_the_stream_tab(qapp, qsettings):
    w = MainWindow(Settings({}), demo=True, qsettings=qsettings)
    try:
        assert len(w._tabs) == 1
        assert w._tabs[0] in w._streamer_tabs
        assert w._tabs[0].youtube_title == "Your Stream"
        assert w._notebook.tabText(0) == "Your Stream"
    finally:
        w.close()


async def test_save_tabs_writes_restore_format(win):
    win._demo = False  # demo never writes tabs; exercise the real save path
    win._close_tab(0)  # drop the auto-created default tab (saves [])
    win.add_tab("xqc", None)
    combo = win.add_tab("", "dQw4w9WgXcQ")
    combo.youtube_target = "dQw4w9WgXcQ"  # real mode sets this when watching
    win._new_streamer_tab()
    assert win._settings.get("tabs") == [
        {"twitch_channel": "xqc"},
        {"twitch_channel": "", "youtube": {"video_id": "dQw4w9WgXcQ"}},
        {"type": "streamer"},
    ]


async def test_close_tab_updates_saved_tabs(win):
    win._demo = False
    win.add_tab("xqc", None)
    assert len(win._settings.get("tabs")) == 2  # default Stream tab + xqc
    win._close_tab(win._notebook.count() - 1)
    assert win._settings.get("tabs") == [{"type": "streamer"}]


async def test_demo_never_writes_saved_tabs(win):
    win._close_tab(0)
    win.add_tab("xqc", None)
    assert win._settings.get("tabs") is None or win._settings.get("tabs") == []


async def test_restore_round_trip_reopens_saved_tabs(qapp, qsettings):
    settings = Settings({})
    w1 = MainWindow(settings, demo=True, qsettings=qsettings)
    try:
        for feed in list(w1._feeds):
            feed.stop()
        w1._feeds.clear()
        w1._demo = False
        w1._close_tab(0)
        w1.add_tab("xqc", None)
        combo = w1.add_tab("", "dQw4w9WgXcQ")
        combo.youtube_target = "dQw4w9WgXcQ"
        w1._new_streamer_tab()
    finally:
        w1.close()
    w2 = MainWindow(settings, demo=True, qsettings=qsettings)
    try:
        for feed in list(w2._feeds):
            feed.stop()
        w2._feeds.clear()
        assert [tab.twitch_channel for tab in w2._tabs] == ["xqc", "", ""]
        assert [tab.youtube_title for tab in w2._tabs] == ["", "dQw4w9WgXcQ", "Your Stream"]
        assert w2._tabs[2] in w2._streamer_tabs
        assert w2._notebook.tabText(2) == "Your Stream"
    finally:
        w2.close()


async def test_restore_streamer_type_creates_streamer_tab(qapp, qsettings):
    w = MainWindow(Settings({"tabs": [{"type": "streamer",
                                       "twitch_channel": "stale_channel"}]}),
                   demo=True, qsettings=qsettings)
    try:
        assert len(w._tabs) == 1
        assert w._tabs[0] in w._streamer_tabs
        # The saved channel is stale: binding re-derives it from live tokens.
        assert w._tabs[0].twitch_channel == ""
        assert w._notebook.tabText(0) == "Your Stream"
    finally:
        w.close()


async def test_restore_legacy_combo_is_plain_tab(qapp, qsettings):
    w = MainWindow(Settings({"tabs": [{"twitch_channel": "xqc",
                                       "youtube": {"video_id": "abc123"}}]}),
                   demo=True, qsettings=qsettings)
    try:
        assert w._tabs[0] not in w._streamer_tabs
        assert w._tabs[0].twitch_channel == "xqc"
        assert w._notebook.tabText(0) == "xqc + abc123"
    finally:
        w.close()
