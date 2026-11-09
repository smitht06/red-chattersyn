"""YQB-28/48/49/57/58: MainWindow renders the YouTube quota wait."""

import datetime as dt
import os
import time

import pytest
from PySide6.QtCore import QSettings

from chattersyn.config.settings import Settings
from chattersyn.services.youtube.quota import Pool
from chattersyn.services.youtube.service import QuotaPoolState, WatchState, YoutubeState
from chattersyn.ui.main_window import MainWindow, _format_resume

RESUME = dt.datetime(2026, 11, 10, 8, 5, tzinfo=dt.UTC)
RESUME_LOCAL = "09:05"  # TZ is pinned to Europe/Berlin (CET, UTC+1) below


# time.tzset is POSIX-only (absent on Windows): the tests that check a local
# resume time pin TZ with it and are skipped where it is missing.
needs_tzset = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="time.tzset is POSIX-only (needed to pin TZ)")


@pytest.fixture
def berlin_time():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


def in_berlin(test):
    """Run `test` with local time pinned to Europe/Berlin (skipped without tzset)."""
    return needs_tzset(pytest.mark.usefixtures("berlin_time")(test))


@pytest.fixture
def qsettings(tmp_path):
    return QSettings(str(tmp_path / "qt.ini"), QSettings.Format.IniFormat)


@pytest.fixture
async def win(qapp, qsettings):
    w = MainWindow(Settings({"tabs": [{"twitch_channel": "chan"}]}), demo=True,
                   qsettings=qsettings)
    for feed in list(w._feeds):
        feed.stop()
    w._feeds.clear()
    yield w
    w.close()


class _FakeSend:
    def __init__(self):
        self.calls = []

    def discard_request(self, key):
        pass

    def send(self, tab_key, text, targets, reply_parent_id=None, reply_parent_login=None):
        self.calls.append((text, targets.twitch_enabled, targets.youtube_enabled))
        return {}


def _yt_tab(win, channel="", target="@handle"):
    tab = win.add_tab(channel, target)
    tab.youtube_target = target  # demo mode skips the watch that normally sets it
    tab.youtube_live_chat_id = "lc1"
    return tab


def _system_texts(tab):
    tab._flush()
    return [m.plain for m in tab.view.message_model.all_messages() if m.is_system]


def _live(tab):
    return YoutubeState(
        watch_key=tab.tab_key, state=WatchState.LIVE, video_id="v1",
        live_chat_id="lc1", title="Stream", channel_title="Chan", channel_id="UC1",
        viewers=7)


@in_berlin
def test_format_resume_is_local_time():
    assert _format_resume(RESUME) == RESUME_LOCAL


@in_berlin
async def test_quota_exhausted_status_shows_resume_time(win):  # YQB-28
    tab = win._tabs[0]
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.QUOTA_EXHAUSTED, resume_at=RESUME))
    assert tab.header._status_label.text() == \
        f"YouTube quota used up · resumes at {RESUME_LOCAL}"


async def test_key_pool_wait_does_not_block_sending(win):  # YQB-27 / ruling
    tab = win._tabs[0]
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.QUOTA_EXHAUSTED, resume_at=RESUME))
    assert tab.header.youtube_enabled()
    assert tab.header._youtube_chip.isEnabled()


@in_berlin
async def test_shared_pool_blocks_toggle_with_resume_tooltip(win):  # YQB-28
    tab = win._tabs[0]
    other = _yt_tab(win, "other")
    win._on_bridge_event(QuotaPoolState(Pool.SHARED, RESUME))
    for t in (tab, other):
        assert not t.header.youtube_enabled()
        assert not t.header._youtube_chip.isEnabled()
        assert t.header._youtube_chip.toolTip() == \
            f"YouTube quota used up — resumes at {RESUME_LOCAL}"
    assert win._shared_pool_resume_at == RESUME


async def test_key_pool_state_leaves_toggle_alone(win):
    tab = win._tabs[0]
    win._on_bridge_event(QuotaPoolState(Pool.KEY, RESUME))
    assert tab.header.youtube_enabled()
    assert tab.header._youtube_chip.isEnabled()
    assert win._shared_pool_resume_at is None


async def test_live_event_keeps_toggle_blocked_while_shared_out(win):  # YQB-57
    tab = win._tabs[0]
    win._on_bridge_event(QuotaPoolState(Pool.SHARED, RESUME))
    win._on_youtube_state(_live(tab))
    assert tab.header._status_label.text().startswith("Chan · 7 viewers")
    assert not tab.header.youtube_enabled()
    assert not tab.header._youtube_chip.isEnabled()
    assert "quota used up" in tab.header._youtube_chip.toolTip()


async def test_live_event_unblocks_when_pool_is_fine(win):
    tab = win._tabs[0]
    tab.header.set_youtube_send_enabled(False, "x")
    win._on_youtube_state(_live(tab))
    assert tab.header.youtube_enabled()


async def test_dual_send_while_waiting_goes_to_twitch_only(win):  # YQB-48
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    win._send = _FakeSend()
    win._on_bridge_event(QuotaPoolState(Pool.SHARED, RESUME))
    win._send_text(tab, "hello")
    assert win._send.calls == [("hello", True, False)]


@in_berlin
async def test_youtube_only_send_while_waiting_is_refused(win):  # YQB-49
    tab = _yt_tab(win)
    win._send = _FakeSend()
    tab.header._twitch_chip.setChecked(False)
    win._on_bridge_event(QuotaPoolState(Pool.SHARED, RESUME))
    win._send_text(tab, "hello")
    assert win._send.calls == []
    texts = _system_texts(tab)
    assert f"YouTube quota used up — resumes at {RESUME_LOCAL}" in texts
    assert not any("nothing to send" in t for t in texts)


async def test_nothing_enabled_without_youtube_target_keeps_old_text(win):
    tab = win._tabs[0]  # Twitch-only tab
    win._send = _FakeSend()
    tab.header._twitch_chip.setChecked(False)
    win._on_bridge_event(QuotaPoolState(Pool.SHARED, RESUME))
    win._send_text(tab, "hello")
    assert win._send.calls == []
    assert any("nothing to send" in t for t in _system_texts(tab))


async def test_toggle_restored_when_shared_pool_resumes(win):  # YQB-58
    tab = win._tabs[0]
    assert tab.header.youtube_enabled()
    win._on_bridge_event(QuotaPoolState(Pool.SHARED, RESUME))
    win._on_bridge_event(QuotaPoolState(Pool.SHARED, None))
    assert tab.header.youtube_enabled()
    assert tab.header._youtube_chip.isEnabled()
    assert tab.header._youtube_chip.toolTip() == "Send to YouTube (toggle)"
    assert win._shared_pool_resume_at is None


async def test_toggle_off_before_stays_off_after_resume(win):  # YQB-58 (A24)
    tab = win._tabs[0]
    tab.header._youtube_chip.setChecked(False)
    win._on_bridge_event(QuotaPoolState(Pool.SHARED, RESUME))
    win._on_bridge_event(QuotaPoolState(Pool.SHARED, None))
    assert tab.header._youtube_chip.isEnabled()
    assert not tab.header.youtube_enabled()


async def test_tab_opened_while_shared_out_starts_blocked(win):
    win._on_bridge_event(QuotaPoolState(Pool.SHARED, RESUME))
    tab = win.add_tab("late", None)
    assert not tab.header._youtube_chip.isEnabled()
    assert not tab.header.youtube_enabled()
