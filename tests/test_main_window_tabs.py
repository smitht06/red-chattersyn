"""Tab titles, per-type new-tab creation (combo/twitch/youtube), the empty
notebook link page, and restore of youtube-only tabs."""

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QInputDialog

from chattersyn.config.settings import Settings
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


async def test_new_twitch_tab_dialog_empty_falls_back_to_forsen(win, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("", True))
    win._new_twitch_tab()
    assert win._tabs[-1].twitch_channel == "forsen"


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


async def test_new_combo_dialog_creates_combo(win, monkeypatch):
    replies = iter([("xqc", True), ("dQw4w9WgXcQ", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: next(replies))
    win._new_tab_dialog()
    tab = win._tabs[-1]
    assert tab.twitch_channel == "xqc"
    assert tab.youtube_title == "dQw4w9WgXcQ"
    assert win._notebook.tabText(win._notebook.indexOf(tab)) == "xqc + dQw4w9WgXcQ"


# -- menu --------------------------------------------------------------------------------


async def test_file_menu_offers_all_three_new_tab_types(win):
    file_menu = win.menuBar().actions()[0].menu()
    texts = [a.text() for a in file_menu.actions() if not a.isSeparator()]
    assert texts[:4] == [
        "New Combo Tab", "New Twitch Tab", "New YouTube Tab", "Close Tab"]


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


async def test_empty_page_combo_link_runs_combo_dialog(win, monkeypatch):
    win._close_tab(0)
    replies = iter([("xqc", True), ("dQw4w9WgXcQ", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: next(replies))
    win._empty_tab_page.new_combo.emit()
    tab = win._tabs[-1]
    assert tab.twitch_channel == "xqc"
    assert tab.youtube_title == "dQw4w9WgXcQ"


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
