"""TwitchService: client-id resolution, Helix token provider, token sync."""

import asyncio

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


# -- deferred join metadata -----------------------------------------------------------------


class _RecordingIrc:
    def __init__(self):
        self.joined = []

    def start(self):
        pass

    def join(self, name):
        self.joined.append(name)

    async def stop(self):
        pass


class _FakeHelix:
    async def close(self):
        pass


async def test_join_desired_kicks_metadata_for_channels_deferred_pre_start(monkeypatch):
    """Regression: channels requested before the service started are only in
    `_desired`; the fresh connection must still load their Twitch emotes, or
    self-sent emotes resolve as raw text for the whole session."""
    service = make_service()
    service._desired.add("alice")
    irc = _RecordingIrc()
    service.irc = irc
    service.helix = _FakeHelix()  # presence is all _kick_metadata checks
    kicked = []
    async def _fake_load(name):
        kicked.append(name)
    monkeypatch.setattr(service, "_load_channel_metadata", _fake_load)
    try:
        service._join_desired()
        await asyncio.sleep(0)  # let the scheduled metadata task run
        assert irc.joined == ["alice"]
        assert kicked == ["alice"]
        service._join_desired()  # idempotent: already loaded
        await asyncio.sleep(0)
        assert kicked == ["alice"]
    finally:
        await service.stop()


async def test_join_channel_on_existing_desired_channel_kicks_metadata(monkeypatch):
    """A channel already in `_desired` (deferred pre-start join) must still
    get its metadata load when join_channel is called again later."""
    service = make_service()
    service._desired.add("alice")
    service.irc = _RecordingIrc()
    service.helix = _FakeHelix()
    kicked = []
    async def _fake_load(name):
        kicked.append(name)
    monkeypatch.setattr(service, "_load_channel_metadata", _fake_load)
    try:
        assert await service.join_channel("alice") is True
        await asyncio.sleep(0)  # let the scheduled metadata task run
        assert kicked == ["alice"]
    finally:
        await service.stop()


async def test_join_desired_skips_metadata_without_helix():
    """Anonymous runs (no Helix) must not attempt a Helix metadata load."""
    service = make_service()
    service._desired.add("alice")
    irc = _RecordingIrc()
    service.irc = irc
    service.helix = None
    try:
        service._join_desired()
        assert irc.joined == ["alice"]
        assert "alice" not in service._metadata_loaded
    finally:
        await service.stop()


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
