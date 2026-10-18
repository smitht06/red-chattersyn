"""TwitchService: client-id resolution, Helix token provider, token sync."""

import httpx
import respx

from chattersyn.config.settings import Settings
from chattersyn.services.twitch.auth import TOKEN_KEY, TOKEN_URL, VALIDATE_URL, TwitchTokens
from chattersyn.services.twitch.helix import BASE
from chattersyn.services.twitch.service import TwitchService


class DictStore:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.deleted = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)
        self.deleted.append(key)


def tokens_json(**overrides):
    t = {
        "access_token": "acc-1",
        "refresh_token": "ref-1",
        "user_id": "",
        "login": "",
        "scopes": ["chat:read", "chat:edit"],
        "expires_at": 9_999_999_999.0,
    }
    t.update(overrides)
    return t


def make_service(settings_data=None, store=None):
    settings = Settings(settings_data or {})
    return TwitchService(settings, store or DictStore())


# -- client-id resolution ------------------------------------------------------------------


def test_client_id_falls_back_to_builtin_default(monkeypatch):
    monkeypatch.setattr(
        "chattersyn.services.twitch.service.DEFAULT_TWITCH_CLIENT_ID", "default-cid")
    service = make_service()
    assert service.auth.client_id == "default-cid"


def test_client_id_settings_override_wins(monkeypatch):
    monkeypatch.setattr(
        "chattersyn.services.twitch.service.DEFAULT_TWITCH_CLIENT_ID", "default-cid")
    service = make_service({"twitch": {"client_id": "override-cid"}})
    assert service.auth.client_id == "override-cid"


def test_client_id_empty_settings_falls_back(monkeypatch):
    monkeypatch.setattr(
        "chattersyn.services.twitch.service.DEFAULT_TWITCH_CLIENT_ID", "default-cid")
    service = make_service({"twitch": {"client_id": ""}})
    assert service.auth.client_id == "default-cid"


# -- Helix token provider -------------------------------------------------------------------


@respx.mock
async def test_user_token_returns_token_and_syncs_service_tokens():
    store = DictStore({TOKEN_KEY: TwitchTokens(**tokens_json()).to_json()})
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={
        "client_id": "cid", "login": "testuser", "user_id": "999",
        "scopes": ["chat:read", "chat:edit"], "expires_in": 3600}))
    service = make_service({"twitch": {"client_id": "cid"}}, store)
    try:
        token = await service._user_token()
    finally:
        await service.stop()
    assert token == "acc-1"
    # self.tokens follows the refresh so the IRC mapper's self_user_id and
    # the settings dialog status stay current.
    assert service.tokens is not None
    assert service.tokens.login == "testuser"


@respx.mock
async def test_user_token_none_when_refresh_fails():
    store = DictStore({TOKEN_KEY: TwitchTokens(**tokens_json()).to_json()})
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400))
    service = make_service({"twitch": {"client_id": "cid"}}, store)
    try:
        assert await service._user_token() is None
    finally:
        await service.stop()
    assert service.tokens is None
    assert TOKEN_KEY in store.deleted
    assert store.get(TOKEN_KEY) is None


@respx.mock
async def test_user_token_none_without_stored_tokens():
    service = make_service({"twitch": {"client_id": "cid"}}, DictStore())
    try:
        assert await service._user_token() is None
    finally:
        await service.stop()
    assert service.tokens is None


# -- Helix lifecycle -------------------------------------------------------------------------


@respx.mock
async def test_ensure_helix_only_with_tokens():
    respx.get(f"{BASE}/chat/badges/global").mock(
        return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{BASE}/chat/emotes/global").mock(
        return_value=httpx.Response(200, json={"data": []}))
    store = DictStore({TOKEN_KEY: TwitchTokens(**tokens_json()).to_json()})
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={
        "client_id": "cid", "login": "testuser", "user_id": "999",
        "scopes": ["chat:read", "chat:edit"], "expires_in": 3600}))
    service = make_service({"twitch": {"client_id": "cid"}}, store)
    try:
        await service.ensure_helix()
        assert service.helix is None  # no tokens synced yet
        service.tokens = await service.auth.ensure_valid()
        await service.ensure_helix()
        assert service.helix is not None
        assert service.helix.client_id == "cid"
    finally:
        await service.stop()


async def test_make_irc_passes_login_as_nick():
    service = make_service()
    service.tokens = TwitchTokens(**tokens_json(login="mylogin", access_token="tok"))
    try:
        irc = service._make_irc()
    finally:
        await service.stop()
    # The wire NICK and self_author() must be the real login — a justinfan
    # nick on an authenticated connection would surface as "you".
    assert irc._nick == "mylogin"
    assert irc._token == "tok"
    assert not irc.is_anonymous


async def test_make_irc_anonymous_keeps_justinfan_nick():
    service = make_service()
    try:
        irc = service._make_irc()
    finally:
        await service.stop()
    assert irc._nick.startswith("justinfan")
    assert irc._token is None
    assert irc.is_anonymous


async def test_disconnect_drops_helix(monkeypatch):
    store = DictStore({TOKEN_KEY: TwitchTokens(**tokens_json()).to_json()})
    service = make_service({"twitch": {"client_id": "cid"}}, store)

    class _FakeHelix:
        client_id = "cid"
        closed = False

        async def close(self):
            self.closed = True

    class _FakeIrc:
        def start(self):
            pass

        def join(self, name):
            pass

        async def stop(self):
            pass

    fake_helix = _FakeHelix()
    service.helix = fake_helix
    monkeypatch.setattr(service, "_make_irc", lambda: _FakeIrc())  # no real IRC connect
    try:
        await service.disconnect()
    finally:
        await service.stop()
    assert service.helix is None
    assert fake_helix.closed
    assert TOKEN_KEY in store.deleted
