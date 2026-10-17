"""M5 live-status wiring: Helix streams endpoint, Twitch poller status
composition, YouTube watch-state offlineAt."""

import datetime as dt

import httpx
import pytest
import respx
from PySide6.QtCore import QSettings

from chattersyn.config.settings import Settings
from chattersyn.services.twitch.helix import BASE, TOKEN_URL, Helix
from chattersyn.services.youtube.service import WatchState, YoutubeState
from chattersyn.ui.main_window import MainWindow
from chattersyn.ui.tab_header import GRAY_DOT, TWITCH_DOT

TOKEN_RESPONSE = {"access_token": "tok-1", "expires_in": 3600}


@pytest.fixture
def qsettings(tmp_path):
    return QSettings(str(tmp_path / "qt.ini"), QSettings.Format.IniFormat)


@pytest.fixture
async def win(qapp, qsettings):
    w = MainWindow(Settings({}), demo=True, qsettings=qsettings)
    for feed in list(w._feeds):
        feed.stop()
    w._feeds.clear()
    yield w
    w.close()


# -- Helix streams ---------------------------------------------------------------------


@respx.mock
async def test_helix_streams_live():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    route = respx.get(f"{BASE}/streams").mock(return_value=httpx.Response(200, json={
        "data": [{"title": "Big Stream", "viewer_count": 1234,
                  "started_at": "2026-10-16T10:00:00Z"}],
    }))
    helix = Helix("cid", "secret")
    try:
        result = await helix.streams("forsen")
    finally:
        await helix.close()
    assert route.calls[0].request.url.params["user_login"] == "forsen"
    assert result == {"title": "Big Stream", "viewer_count": 1234,
                      "started_at": "2026-10-16T10:00:00Z"}


@respx.mock
async def test_helix_streams_offline_is_none():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(f"{BASE}/streams").mock(return_value=httpx.Response(200, json={"data": []}))
    helix = Helix("cid", "secret")
    try:
        assert await helix.streams("forsen") is None
    finally:
        await helix.close()


# -- status formatting -------------------------------------------------------------------


def test_format_stream_status_full():
    text = MainWindow._format_stream_status("Big Stream", 1234,
                                            "2026-10-16T10:00:00Z")
    assert text.startswith("Big Stream · 1,234 viewers · up ")


def test_format_stream_status_parts_missing():
    assert MainWindow._format_stream_status("", 0, "") == "live"
    assert MainWindow._format_stream_status("Only Title", 0, "") == "Only Title"
    assert MainWindow._format_stream_status("", 42, "") == "42 viewers"
    assert MainWindow._format_stream_status("", 0, "2026-10-16T10:00:00Z").startswith("up ")


def test_format_stream_status_malformed_started_at():
    assert MainWindow._format_stream_status("T", 1, "not-a-date") == "T · 1 viewers"


# -- poller application --------------------------------------------------------------------


async def test_apply_twitch_stream_live_sets_dot_and_status(win):
    tab = win._tabs[0]
    win._apply_twitch_stream("forsen", {
        "title": "Big Stream", "viewer_count": 1234, "started_at": "2026-10-16T10:00:00Z"})
    assert TWITCH_DOT in tab.header._twitch_label.text()
    assert tab.header._status_label.text().startswith("Big Stream · 1,234 viewers · up ")


async def test_apply_twitch_stream_offline_without_prior_live(win):
    tab = win._tabs[0]
    win._apply_twitch_stream("forsen", None)
    assert GRAY_DOT in tab.header._twitch_label.text()
    assert tab.header._status_label.text() == ""


async def test_apply_twitch_stream_offline_after_live_records_offline_at(win):
    tab = win._tabs[0]
    win._apply_twitch_stream("forsen", {"title": "T", "viewer_count": 1, "started_at": ""})
    win._apply_twitch_stream("forsen", None)
    assert GRAY_DOT in tab.header._twitch_label.text()
    assert tab.header._status_label.text().startswith("offline since ")
    first = tab.header._status_label.text()
    win._apply_twitch_stream("forsen", None)  # stays frozen at the first time
    assert tab.header._status_label.text() == first


async def test_apply_twitch_stream_back_live_clears_offline(win):
    tab = win._tabs[0]
    win._apply_twitch_stream("forsen", {"title": "T", "viewer_count": 1, "started_at": ""})
    win._apply_twitch_stream("forsen", None)
    assert "offline since" in tab.header._status_label.text()
    win._apply_twitch_stream("forsen", {"title": "T", "viewer_count": 2, "started_at": ""})
    assert "offline since" not in tab.header._status_label.text()
    assert "2 viewers" in tab.header._status_label.text()


async def test_apply_twitch_stream_updates_only_matching_tabs(win):
    win.add_tab("other", None)
    for feed in win._feeds:
        feed.stop()
    win._apply_twitch_stream("forsen", {"title": "T", "viewer_count": 9, "started_at": ""})
    assert "9 viewers" in win._tabs[0].header._status_label.text()
    assert win._tabs[1].header._status_label.text() == ""
    assert TWITCH_DOT in win._tabs[0].header._twitch_label.text()
    assert GRAY_DOT in win._tabs[1].header._twitch_label.text()


# -- YouTube watch-state offlineAt + composition -------------------------------------------


async def test_youtube_ended_status_carries_offline_at(win):
    tab = win._tabs[0]
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.LIVE, video_id="v1",
        live_chat_id="lc1", title="Stream", channel_title="Chan", channel_id="UC1"))
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.ENDED, video_id="v1",
        live_chat_id="lc1", title="Stream", channel_title="Chan", channel_id="UC1"))
    assert tab.header._status_label.text().startswith("stream ended · offline since ")


async def test_status_composition_joins_both_platforms(win):
    tab = win._tabs[0]
    win._apply_twitch_stream("forsen", {"title": "T", "viewer_count": 5, "started_at": ""})
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.LIVE, video_id="v1",
        live_chat_id="lc1", title="Stream", channel_title="Chan", channel_id="UC1"))
    assert tab.header._status_label.text() == "T · 5 viewers · Chan"


async def test_youtube_live_status_composes_stats(win):
    tab = win._tabs[0]
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.LIVE, video_id="v1",
        live_chat_id="lc1", title="Stream", channel_title="Chan", channel_id="UC1",
        viewers=1234, started_at=dt.datetime(2026, 10, 16, 10, 0, tzinfo=dt.UTC)))
    assert tab.header._status_label.text().startswith("Chan · 1,234 viewers · up ")


# -- YouTube send-toggle blocking (quota / auth) -----------------------------------------


async def test_quota_exhausted_blocks_youtube_send_toggle(win):
    tab = win._tabs[0]
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.QUOTA_EXHAUSTED,
        video_id="v1", live_chat_id="lc1", channel_id="UC1"))
    assert not tab.header.youtube_enabled()
    assert not tab.header._youtube_chip.isEnabled()
    assert "quota" in tab.header._youtube_chip.toolTip()
    assert tab.header._status_label.text() == "YouTube quota exhausted"


async def test_live_unblocks_youtube_send_toggle(win):
    tab = win._tabs[0]
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.QUOTA_EXHAUSTED, video_id="v1"))
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.LIVE, video_id="v1",
        live_chat_id="lc1", title="Stream", channel_title="Chan", channel_id="UC1"))
    assert tab.header.youtube_enabled()
    assert tab.header._youtube_chip.isEnabled()
    assert tab.header._youtube_chip.toolTip() == "Send to YouTube (toggle)"


async def test_auth_required_blocks_youtube_send_toggle(win):
    tab = win._tabs[0]
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.AUTH_REQUIRED, video_id="v1"))
    assert not tab.header.youtube_enabled()
    assert not tab.header._youtube_chip.isEnabled()
    assert tab.header._status_label.text() == "YouTube auth required"


async def test_unblock_restores_user_toggle_off_preference(win):
    """The user had YouTube send off before the block — unblocking must not
    force it back on."""
    tab = win._tabs[0]
    tab.header._youtube_chip.setChecked(False)  # user choice
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.QUOTA_EXHAUSTED, video_id="v1"))
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.LIVE, video_id="v1",
        live_chat_id="lc1", title="Stream", channel_title="Chan", channel_id="UC1"))
    assert tab.header._youtube_chip.isEnabled()
    assert not tab.header.youtube_enabled()  # stays off per the user's choice


async def test_youtube_auth_state_blocks_and_unblocks(win):
    tab = win._tabs[0]
    win._on_auth_state("youtube", False, "token expired")
    assert not tab.header.youtube_enabled()
    assert not tab.header._youtube_chip.isEnabled()
    assert tab.header._youtube_chip.toolTip() == "token expired"
    win._on_auth_state("youtube", True, "connected")
    assert tab.header.youtube_enabled()
    assert tab.header._youtube_chip.isEnabled()
