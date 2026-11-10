"""THM-5, THM-6, THM-8, THM-11: MainWindow runs Helix moderation commands."""

import asyncio

import httpx
import pytest
import respx
from PySide6.QtCore import QSettings

from chattersyn.config.settings import Settings
from chattersyn.models.message import Author, Platform
from chattersyn.services.commands import CommandController
from chattersyn.services.twitch import auth
from chattersyn.services.twitch.helix import BASE, Helix
from chattersyn.services.twitch.mod_commands import HelixCommand
from chattersyn.ui.main_window import MainWindow
from chattersyn.ui.usercard import UsercardTarget

BASE_SCOPES = ["chat:read", "chat:edit"]
USERS = {
    "alice": {"id": "2", "login": "alice", "display_name": "alice"},
    "bob": {"id": "1", "login": "bob", "display_name": "Bob"},
}


class _Service:
    def __init__(self, scopes):
        async def token():
            return "tok"
        self.helix = Helix("cid", token)
        self.tokens = auth.TwitchTokens(access_token="tok", refresh_token="r", login="me",
                                        user_id="7", scopes=list(scopes))

    def self_author(self):
        return Author(name="me", login="me", user_id="7")


@pytest.fixture
def qsettings(tmp_path):
    return QSettings(str(tmp_path / "qt.ini"), QSettings.Format.IniFormat)


@pytest.fixture
async def win(qapp, qsettings):
    w = MainWindow(Settings({"tabs": [{"twitch_channel": "alice"}]}), demo=True,
                   qsettings=qsettings)
    for feed in list(w._feeds):
        feed.stop()
    w._feeds.clear()
    yield w
    if w._twitch is not None:
        await w._twitch.helix.close()
    w.close()


def _users_route():
    def answer(request):
        user = USERS.get(request.url.params.get("login"))
        return httpx.Response(200, json={"data": [user] if user else []})
    return respx.get(f"{BASE}/users").mock(side_effect=answer)


def _rows(tab):
    tab.message_model.flush()
    return [m.plain for m in tab.message_model.all_messages() if m.is_system]


async def test_confirmation_row_added(win):
    """Success adds one system row."""
    with respx.mock:
        _users_route()
        route = respx.post(f"{BASE}/chat/shoutouts").mock(return_value=httpx.Response(204))
        win._twitch = _Service([*BASE_SCOPES, auth.SHOUTOUTS_SCOPE])
        tab = win._tabs[0]
        await win._twitch_helix_command(tab, HelixCommand("shoutout", user="bob"))
        assert route.called
        assert _rows(tab) == ["You gave bob a shoutout."]


async def test_custom_action_runs_helix_command(win):
    """THM-5: a card action's text goes through the Helix path."""
    with respx.mock:
        _users_route()
        route = respx.post(f"{BASE}/chat/shoutouts").mock(return_value=httpx.Response(204))
        win._twitch = _Service([*BASE_SCOPES, auth.SHOUTOUTS_SCOPE])
        win._commands = CommandController(win._settings, [])  # demo mode has none
        tab = win._tabs[0]
        target = UsercardTarget(platform=Platform.TWITCH, login="bob", display_name="Bob",
                                user_id="1", channel_id="alice")
        win._on_usercard_action(tab, target, "/shoutout bob")
        for _ in range(200):
            if route.called:
                break
            await asyncio.sleep(0.01)
        assert route.called


async def test_old_login_asks_to_reconnect(win):
    """THM-6: a login without the scope sends nothing."""
    with respx.mock:
        users = _users_route()
        route = respx.post(f"{BASE}/chat/shoutouts").mock(return_value=httpx.Response(204))
        win._twitch = _Service(BASE_SCOPES)
        tab = win._tabs[0]
        await win._twitch_helix_command(tab, HelixCommand("shoutout", user="bob"))
        assert not route.called and not users.called
        assert _rows(tab) == [
            "Reconnect your Twitch account (Connect Twitch Account…) "
            "to grant ChatterSyn permission for /shoutout."]


async def test_not_signed_in(win):
    """THM-8."""
    win._twitch = None
    tab = win._tabs[0]
    await win._twitch_helix_command(tab, HelixCommand("uptime"))
    assert _rows(tab) == ["Connect a Twitch account to moderate."]


async def test_twitch_refusal_row(win):
    """THM-11: Twitch's message is shown."""
    with respx.mock:
        _users_route()
        respx.post(f"{BASE}/raids").mock(return_value=httpx.Response(
            400, json={"message": "The broadcaster is not live."}))
        win._twitch = _Service([*BASE_SCOPES, auth.RAIDS_SCOPE])
        tab = win._tabs[0]
        await win._twitch_helix_command(tab, HelixCommand("raid", user="bob"))
        assert _rows(tab) == ["Twitch /raid failed: The broadcaster is not live."]


async def test_raid_resolves_target_channel_id(win):
    """Review focus 3: broadcaster = tab channel, target resolved by login."""
    with respx.mock:
        _users_route()
        route = respx.post(f"{BASE}/raids").mock(return_value=httpx.Response(200, json={}))
        win._twitch = _Service([*BASE_SCOPES, auth.RAIDS_SCOPE])
        tab = win._tabs[0]
        await win._twitch_helix_command(tab, HelixCommand("raid", user="bob"))
        params = route.calls.last.request.url.params
        assert params["from_broadcaster_id"] == "2"
        assert params["to_broadcaster_id"] == "1"


async def test_click_opens_usercard(win, monkeypatch):
    """THM-16/18: the popup opens titled, and a click opens that user's card."""
    from chattersyn.ui.user_list_popup import UserListPopup
    monkeypatch.setattr(UserListPopup, "show_at_cursor", lambda self: None)
    opened = []
    monkeypatch.setattr(win, "_open_usercard_by_login",
                        lambda tab, login: opened.append((tab, login)))
    tab = win._tabs[0]
    win._show_user_list(tab, "mods", ["carol", "bob"])
    popup = win._user_list_popup
    assert popup.title_text() == "Moderators (2)"
    assert popup.visible_names() == ["bob", "carol"]
    popup.user_clicked.emit("bob")
    assert opened == [(tab, "bob")]


async def test_empty_vips_popup_text(win, monkeypatch):
    from chattersyn.ui.user_list_popup import UserListPopup
    monkeypatch.setattr(UserListPopup, "show_at_cursor", lambda self: None)
    win._show_user_list(win._tabs[0], "vips", [])
    assert win._user_list_popup.title_text() == "VIPs (0)"
    assert win._user_list_popup.empty_label.text() == "No VIPs."


async def test_mod_refused_shows_row_no_popup(win):
    """THM-24: a refused /vips is a failure row and no popup."""
    with respx.mock:
        _users_route()
        respx.get(f"{BASE}/channels/vips").mock(return_value=httpx.Response(
            403, json={"message": "Only the broadcaster can view VIPs."}))
        win._twitch = _Service([*BASE_SCOPES, auth.VIPS_SCOPE])
        tab = win._tabs[0]
        await win._twitch_helix_command(tab, HelixCommand("vips"))
        assert win._user_list_popup is None
        assert _rows(tab) == ["Twitch /vips failed: Only the broadcaster can view VIPs."]
