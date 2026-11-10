"""TSR-10/11/25: MainWindow renders Twitch reachability and sign-in rejections."""

import types

import pytest
from PySide6.QtCore import QSettings

from chattersyn.config.settings import Settings
from chattersyn.core.bus import bridge
from chattersyn.services.twitch.auth import TwitchTokens
from chattersyn.services.twitch.service import EXPIRED_MESSAGE, REJECTED_MESSAGE
from chattersyn.ui.main_window import MainWindow

RETRYING = "Twitch: can't reach Twitch — retrying…"


@pytest.fixture
def qsettings(tmp_path):
    return QSettings(str(tmp_path / "qt.ini"), QSettings.Format.IniFormat)


@pytest.fixture
async def win(qapp, qsettings):
    w = MainWindow(Settings({"tabs": [{"twitch_channel": "chan"}, {"twitch_channel": "two"}]}),
                   demo=True, qsettings=qsettings)
    for feed in list(w._feeds):
        feed.stop()
    w._feeds.clear()
    yield w
    w.close()


def _event(state, detail=""):
    return {"platform": "twitch", "channel_id": "", "state": state, "detail": detail}


def _status(win):
    return win.statusBar().currentMessage()


def _system_texts(tab):
    tab._flush()
    return [m.plain for m in tab.view.message_model.all_messages() if m.is_system]


class _Service:
    """Fake TwitchService: start() reports an unreachable Twitch."""

    helix = None

    def __init__(self, tokens=None, on_event=None):
        self.tokens = tokens
        self.unreachable = False
        self.started = False
        self._on_event = on_event

    async def start(self):
        self.started = True
        self.unreachable = True
        self._on_event(_event("unreachable"))


async def test_launch_unreachable_shows_retrying_without_rows(win):  # TSR-10
    service = _Service(TwitchTokens(access_token="a", refresh_token="r", login="red"),
                       on_event=win._on_connection_state)
    win._twitch = service
    before = [_system_texts(t) for t in win._tabs]
    await win._start_services()
    assert service.started
    assert _status(win) == RETRYING
    assert [_system_texts(t) for t in win._tabs] == before
    assert win._twitch_account_status() == "Connected as red"


async def test_repeated_unreachable_is_idempotent(win):
    win._on_connection_state(_event("unreachable"))
    before = [_system_texts(t) for t in win._tabs]
    win._on_connection_state(_event("unreachable"))
    win._on_connection_state(_event("unreachable"))
    assert _status(win) == RETRYING
    assert [_system_texts(t) for t in win._tabs] == before


async def test_signed_in_connect_after_retrying_shows_connected(win):  # TSR-11
    win._on_connection_state(_event("unreachable"))
    win._on_connection_state(_event("connected", "signed_in"))
    assert _status(win) == "Twitch connected"
    assert not win._twitch_retrying


async def test_connect_without_retrying_keeps_status(win):
    win.statusBar().showMessage("something else")
    win._on_connection_state(_event("connected", "signed_in"))
    assert _status(win) == "something else"


async def test_anonymous_connect_keeps_retrying_text(win):
    win._on_connection_state(_event("unreachable"))
    win._on_connection_state(_event("connected", "anonymous"))
    assert _status(win) == RETRYING


async def test_other_platform_events_are_ignored(win):
    win._on_connection_state({"platform": "youtube", "state": "unreachable"})
    assert not win._twitch_retrying


async def test_rejection_outranks_retrying(win):  # TSR-25
    win._on_connection_state(_event("unreachable"))
    win._on_auth_state("twitch", False, EXPIRED_MESSAGE)
    win._on_connection_state(_event("connected", "anonymous"))
    assert _status(win) == EXPIRED_MESSAGE
    assert "Twitch connected" not in _status(win)
    for tab in win._tabs:
        assert _system_texts(tab).count(EXPIRED_MESSAGE) == 1


async def test_launch_status_does_not_overwrite_rejection(win):  # TSR-25 via start()
    class Rejecting(_Service):
        async def start(self):
            self.unreachable = False
            win._on_auth_state("twitch", False, EXPIRED_MESSAGE)

    win._twitch = Rejecting()
    await win._start_services()
    assert _status(win) == EXPIRED_MESSAGE


async def test_launch_status_shown_when_reachable(win):
    class Fine(_Service):
        async def start(self):
            self.unreachable = False

    win._twitch = Fine()
    await win._start_services()
    assert _status(win).startswith("Twitch connected (read-only")


async def test_old_texts_keep_their_prefix(win):
    win._on_auth_state("twitch", False, "signed out — read-only mode")
    assert _status(win) == "twitch: signed out — read-only mode"
    assert "twitch: signed out — read-only mode" in _system_texts(win._tabs[0])


async def test_settings_row_shows_problem_until_signed_in_connect(win):
    win._twitch = types.SimpleNamespace(tokens=None)
    assert win._twitch_account_status() == "Not connected"
    win._on_auth_state("twitch", False, EXPIRED_MESSAGE)
    assert win._twitch_account_status() == f"Not connected — {EXPIRED_MESSAGE}"
    win._on_connection_state(_event("connected", "anonymous"))
    assert win._twitch_account_status() == f"Not connected — {EXPIRED_MESSAGE}"
    win._on_connection_state(_event("connected", "signed_in"))
    assert win._twitch_account_status() == "Not connected"


async def test_successful_connect_flow_clears_problem(win):
    win._twitch = types.SimpleNamespace(tokens=None)
    win._on_auth_state("twitch", False, EXPIRED_MESSAGE)
    win._on_auth_state("twitch", True, "connected")
    assert win._twitch_account_status() == "Not connected"


async def test_problem_ignored_while_service_holds_tokens(win):
    win._twitch = types.SimpleNamespace(tokens=TwitchTokens(access_token="a", login="red"))
    win._on_auth_state("twitch", False, EXPIRED_MESSAGE)
    assert win._twitch_account_status() == "Connected as red"


async def test_bridge_signal_is_wired(qapp, qsettings, monkeypatch):
    from chattersyn.ui import main_window as mw

    async def noop(self):
        pass

    monkeypatch.setattr(mw.MainWindow, "_start_services", noop)
    w = MainWindow(Settings({"tabs": [{"twitch_channel": "chan"}]}), demo=False,
                   qsettings=qsettings)
    try:
        bridge.connection_state.emit(_event("unreachable"))
        assert _status(w) == RETRYING
    finally:
        w.close()


@pytest.mark.parametrize("text", [
    "signed out — read-only mode",
    "sign-in failed or timed out",
    "sign-in succeeded but the token refresh failed — try connecting again",
])
async def test_unannounced_failures_stay_out_of_the_account_row(win, text):
    win._twitch = types.SimpleNamespace(tokens=None)
    win._on_connection_state(_event("unreachable"))
    win._on_auth_state("twitch", False, text)
    assert win._twitch_account_status() == "Not connected"
    assert not win._twitch_retrying
    assert win.statusBar().currentMessage() == f"twitch: {text}"


async def test_rejected_message_is_stored(win):
    win._twitch = types.SimpleNamespace(tokens=None)
    win._on_auth_state("twitch", False, REJECTED_MESSAGE)
    assert win._twitch_account_status() == f"Not connected — {REJECTED_MESSAGE}"
