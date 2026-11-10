"""TUF-1..8: MainWindow fills a signed-in Twitch user card, one lookup at a time."""

import asyncio

import httpx
import pytest
import respx
from PySide6.QtCore import QSettings

from chattersyn.config.settings import Settings
from chattersyn.models.message import Author, Platform
from chattersyn.services.twitch.auth import FOLLOWERS_SCOPE, MODERATION_SCOPE, TwitchTokens
from chattersyn.services.twitch.helix import BASE, Helix
from chattersyn.ui.main_window import MainWindow
from chattersyn.ui.usercard import UsercardPopup, UsercardTarget

ALL_SCOPES = ["chat:read", "chat:edit", MODERATION_SCOPE, FOLLOWERS_SCOPE]
USERS = {
    "bob": {"id": "1", "login": "bob", "display_name": "Bob",
            "profile_image_url": "https://img/bob.png", "created_at": "2019-01-02T03:04:05Z"},
    "alice": {"id": "2", "login": "alice", "display_name": "alice",
              "profile_image_url": "https://img/alice.png", "created_at": "2015-06-07T00:00:00Z"},
}


class _Service:
    """Fake TwitchService: a real Helix client over respx, signed in as `me`."""

    def __init__(self, scopes):
        async def token():
            return "tok"
        self.helix = Helix("cid", token)
        self.tokens = TwitchTokens(access_token="tok", refresh_token="r", login="me",
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
        login = request.url.params.get("login")
        user = USERS.get(login)
        return httpx.Response(200, json={"data": [user] if user else []})
    return respx.get(f"{BASE}/users").mock(side_effect=answer)


def _total_route(status=200, total=4321):
    return respx.get(f"{BASE}/channels/followers", params={"broadcaster_id": "1"}).mock(
        return_value=httpx.Response(status, json={"total": total}))


def _follow_route(*, status=200, followed_at="2024-03-05T10:00:00Z"):
    rows = [{"user_id": "1", "user_login": "bob", "followed_at": followed_at}] if followed_at else []
    return respx.get(f"{BASE}/channels/followers",
                     params={"broadcaster_id": "2", "user_id": "1"}).mock(
        return_value=httpx.Response(status, json={"total": 99, "data": rows}))


async def _open(win, *, login="bob", scopes=ALL_SCOPES, self_is_mod=True):
    win._twitch = _Service(scopes)
    user = USERS[login]
    target = UsercardTarget(platform=Platform.TWITCH, login=login, display_name=user["display_name"],
                            user_id=user["id"], channel_id="alice")
    popup = UsercardPopup(win._settings, target, [], self_is_mod=self_is_mod, parent=win)
    win._usercard_popup = popup
    await win._enrich_usercard(popup, target, "alice", self_is_mod)
    return popup


def _shown(row):
    return not row.isHidden()


@respx.mock
async def test_card_fills_in_when_signed_in(win):
    """TUF-1."""
    _users_route()
    _total_route()
    _follow_route()
    popup = await _open(win)
    assert popup._avatar_url == "https://img/bob.png"
    assert popup._created_value.text() == "2019-01-02"
    assert popup._followers_value.text() == "4,321"
    assert _shown(popup._created_row) and _shown(popup._followers_row)


@respx.mock
async def test_follow_lookup_failure_keeps_other_details(win):
    """TUF-2."""
    _users_route()
    _total_route()
    _follow_route(status=500)
    popup = await _open(win)
    assert _shown(popup._created_row) and _shown(popup._followers_row)
    assert popup._avatar_url == "https://img/bob.png"
    assert not _shown(popup._followage_row)


@respx.mock
async def test_follower_count_failure_keeps_other_details(win):
    """TUF-3."""
    _users_route()
    _total_route(status=503)
    _follow_route()
    popup = await _open(win)
    assert _shown(popup._created_row)
    assert popup._avatar_url == "https://img/bob.png"
    assert not _shown(popup._followers_row)
    assert _shown(popup._followage_row)


@respx.mock
async def test_mod_sees_followed_since(win):
    """TUF-4."""
    _users_route()
    _total_route()
    _follow_route()
    popup = await _open(win)
    assert _shown(popup._followage_row)
    assert popup._followage_key.text() == "Followed alice since:"
    assert popup._followage_value.text() == "2024-03-05"


@respx.mock
async def test_not_following_shows_no_row(win):
    """TUF-5."""
    _users_route()
    _total_route()
    _follow_route(followed_at=None)
    popup = await _open(win)
    assert not _shown(popup._followage_row)


@respx.mock
async def test_no_mod_rights_sends_no_follow_lookup(win):
    """TUF-6."""
    _users_route()
    _total_route()
    follow = _follow_route()
    popup = await _open(win, self_is_mod=False)
    assert follow.call_count == 0
    assert not _shown(popup._followage_row)
    assert _shown(popup._followers_row)


@respx.mock
async def test_missing_scope_sends_no_follow_lookup(win):
    """TUF-7: a token from before the scope was added (synced from /validate)."""
    _users_route()
    _total_route()
    follow = _follow_route()
    popup = await _open(win, scopes=["chat:read", "chat:edit", MODERATION_SCOPE])
    assert follow.call_count == 0
    assert not _shown(popup._followage_row)
    assert _shown(popup._followers_row)


@respx.mock
async def test_broadcaster_card_sends_no_follow_lookup(win):
    """TUF-8: alice's own card in alice's channel."""
    _users_route()
    respx.get(f"{BASE}/channels/followers", params={"broadcaster_id": "2"}).mock(
        return_value=httpx.Response(200, json={"total": 99, "data": []}))
    popup = await _open(win, login="alice")
    followers_calls = [c for c in respx.calls if c.request.url.path.endswith("/channels/followers")]
    assert all("user_id" not in c.request.url.params for c in followers_calls)
    assert not _shown(popup._followage_row)
    assert popup._followers_value.text() == "99"


@respx.mock
async def test_late_answer_after_card_replaced_is_dropped(win):
    """Review Focus 1: a card replaced mid-lookup gets nothing written into it."""
    _users_route()
    _total_route()
    _follow_route()
    win._twitch = _Service(ALL_SCOPES)
    target = UsercardTarget(platform=Platform.TWITCH, login="bob", display_name="Bob",
                            user_id="1", channel_id="alice")
    old = UsercardPopup(win._settings, target, [], self_is_mod=True, parent=win)
    win._usercard_popup = UsercardPopup(win._settings, target, [], self_is_mod=True, parent=win)
    await win._enrich_usercard(old, target, "alice", True)
    assert not _shown(old._created_row)
    assert not _shown(old._followers_row)
    assert not _shown(old._followage_row)


@respx.mock
async def test_failed_user_lookup_keeps_other_rows(win):
    """TUF-2/3: a failing /users lookup hides only its own details."""
    respx.get(f"{BASE}/users", params={"login": "alice"}).mock(
        return_value=httpx.Response(200, json={"data": [USERS["alice"]]}))
    respx.get(f"{BASE}/users", params={"login": "bob"}).mock(
        return_value=httpx.Response(503, json={}))
    _total_route()
    _follow_route()
    win._twitch = _Service(ALL_SCOPES)
    target = UsercardTarget(platform=Platform.TWITCH, login="bob", display_name="Bob",
                            user_id="1", channel_id="alice")
    popup = UsercardPopup(win._settings, target, [], self_is_mod=True, parent=win)
    win._usercard_popup = popup
    await win._enrich_usercard(popup, target, "alice", True)
    assert not _shown(popup._created_row)
    assert popup._followers_value.text() == "4,321"
    assert popup._followage_value.text() == "2024-03-05"


@respx.mock
async def test_follow_row_does_not_wait_for_follower_count(win):
    """TUF-1: each row fills as its own lookup answers."""
    _users_route()
    release = asyncio.Event()

    async def slow_total(request):
        await release.wait()
        return httpx.Response(200, json={"total": 5})
    respx.get(f"{BASE}/channels/followers", params={"broadcaster_id": "1"}).mock(
        side_effect=slow_total)
    _follow_route()
    win._twitch = _Service(ALL_SCOPES)
    target = UsercardTarget(platform=Platform.TWITCH, login="bob", display_name="Bob",
                            user_id="1", channel_id="alice")
    popup = UsercardPopup(win._settings, target, [], self_is_mod=True, parent=win)
    win._usercard_popup = popup
    task = asyncio.ensure_future(win._enrich_usercard(popup, target, "alice", True))
    try:
        for _ in range(50):
            if _shown(popup._followage_row):
                break
            await asyncio.sleep(0.01)
        assert _shown(popup._followage_row)
        assert not _shown(popup._followers_row)
    finally:
        release.set()
        await task
    assert popup._followers_value.text() == "5"


@respx.mock
async def test_card_replaced_mid_lookup_gets_no_late_rows(win):
    """Review Focus 1: the card is replaced while the follower lookup runs."""
    _users_route()
    target = UsercardTarget(platform=Platform.TWITCH, login="bob", display_name="Bob",
                            user_id="1", channel_id="alice")
    win._twitch = _Service(ALL_SCOPES)
    old = UsercardPopup(win._settings, target, [], self_is_mod=True, parent=win)
    win._usercard_popup = old

    def replace_then_answer(request):
        win._usercard_popup = UsercardPopup(win._settings, target, [], parent=win)
        return httpx.Response(200, json={"total": 5})
    respx.get(f"{BASE}/channels/followers", params={"broadcaster_id": "1"}).mock(
        side_effect=replace_then_answer)
    _follow_route()
    await win._enrich_usercard(old, target, "alice", True)
    assert _shown(old._created_row)  # answered before the swap
    assert not _shown(old._followers_row)
    assert not _shown(old._followage_row)


async def test_show_usercard_passes_tab_channel_and_mod_rights(win, monkeypatch):
    """The card-opening code hands the tab's channel and mod rights on."""
    from chattersyn.models.message import Message
    seen = {}

    async def fake_enrich(popup, target, channel, self_is_mod):
        seen.update(channel=channel, self_is_mod=self_is_mod)
    monkeypatch.setattr(win, "_enrich_usercard", fake_enrich)
    win._moderation.set_twitch_mod("alice", True)
    msg = Message.text(Platform.TWITCH, "alice", Author(name="Bob", login="bob", user_id="1"), "hi")
    win._show_usercard(msg, "bob")
    await asyncio.sleep(0)
    assert seen == {"channel": "alice", "self_is_mod": True}
    win._usercard_popup.close()
