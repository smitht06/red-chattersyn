"""YQB-15/17/18: MainWindow reports which tab is on screen to the YouTube service."""

import pytest
from PySide6.QtCore import QEvent, QSettings

from chattersyn.config.settings import Settings
from chattersyn.ui.main_window import MainWindow


class _FakeYouTube:
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []
        self.watched: list[tuple[str, str]] = []

    def set_on_screen(self, watch_key, on_screen):
        self.calls.append((watch_key, on_screen))

    def watch(self, target, key):
        self.watched.append((target, key))

    def unwatch(self, key):
        pass

    def last(self) -> dict[str, bool]:
        return dict(self.calls)


@pytest.fixture
def qsettings(tmp_path):
    return QSettings(str(tmp_path / "qt.ini"), QSettings.Format.IniFormat)


@pytest.fixture
async def win(qapp, qsettings):
    w = MainWindow(Settings({"tabs": []}), demo=True, qsettings=qsettings)
    for feed in list(w._feeds):
        feed.stop()
    w._feeds.clear()
    w._youtube = _FakeYouTube()
    yield w
    for feed in list(w._feeds):
        feed.stop()
    w.close()


def _yt_tab(win, channel, target):
    tab = win.add_tab(channel, target)
    tab.youtube_target = target  # demo mode skips the watch that normally sets it
    return tab


def _two_tabs(win):
    a = _yt_tab(win, "chan_a", "@handlea")
    b = _yt_tab(win, "chan_b", "@handleb")
    win._notebook.setCurrentWidget(a)
    win._youtube.calls.clear()
    return a, b


def test_selecting_tab_b_reports_a_off_and_b_on(win):  # YQB-17
    a, b = _two_tabs(win)
    win._notebook.setCurrentWidget(b)
    assert win._youtube.last() == {a.tab_key: False, b.tab_key: True}


def test_minimize_reports_every_tab_off(win, monkeypatch):  # YQB-15
    a, b = _two_tabs(win)
    monkeypatch.setattr(win, "isMinimized", lambda: True)
    win.changeEvent(QEvent(QEvent.Type.WindowStateChange))
    assert win._youtube.last() == {a.tab_key: False, b.tab_key: False}


def test_restore_reports_selected_tab_on(win, monkeypatch):  # YQB-18
    a, b = _two_tabs(win)
    monkeypatch.setattr(win, "isMinimized", lambda: True)
    win.changeEvent(QEvent(QEvent.Type.WindowStateChange))
    monkeypatch.setattr(win, "isMinimized", lambda: False)
    win.changeEvent(QEvent(QEvent.Type.WindowStateChange))
    assert win._youtube.last() == {a.tab_key: True, b.tab_key: False}


def test_new_background_tab_reported_off_after_watch(win):
    a, _ = _two_tabs(win)
    win._notebook.setCurrentWidget(a)
    win._youtube.calls.clear()
    c = _yt_tab(win, "chan_c", "@handlec")
    win._notebook.setCurrentWidget(a)
    assert win._youtube.last()[c.tab_key] is False
    assert win._youtube.last()[a.tab_key] is True


def test_rewatch_streamer_tab_reported_again(win):
    a, b = _two_tabs(win)
    win._streamer_tabs.append(b)
    win._youtube.calls.clear()
    win._rewatch_streamer_tabs()
    assert win._youtube.last()[b.tab_key] is False


def test_tabs_without_youtube_target_ignored(win):
    t = win.add_tab("plain", None)
    win._youtube.calls.clear()
    win._report_visibility()
    assert t.tab_key not in win._youtube.last()


def test_no_youtube_service_does_nothing(win):
    win._youtube = None
    _yt_tab(win, "chan_a", "@handlea")
    win._report_visibility()  # must not raise
