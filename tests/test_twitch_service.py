"""TwitchService: client-id resolution, Helix token provider, token sync."""

import asyncio

import httpx
import pytest
import respx
from fakeirc import FakeIrcServer

from chattersyn.config.settings import Settings
from chattersyn.core.bus import bridge
from chattersyn.services.twitch import service as service_module
from chattersyn.services.twitch.auth import (
    TOKEN_KEY,
    TOKEN_URL,
    VALIDATE_URL,
    TwitchTokens,
    TwitchUnreachable,
)
from chattersyn.services.twitch.helix import BASE, Helix, HelixError
from chattersyn.services.twitch.irc import TwitchIrc
from chattersyn.services.twitch.mapper import AuthFailed
from chattersyn.services.twitch.service import EXPIRED_MESSAGE, REJECTED_MESSAGE, TwitchService


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


# -- launch ---------------------------------------------------------------------------------


class _LaunchIrc:
    """Stands in for TwitchIrc: records how the service built and drove it."""

    built: list = []

    def __init__(self, mapper, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.joined = []
        type(self).built.append(self)

    @property
    def is_anonymous(self):
        return not self.kwargs.get("token")

    def start(self):
        self.started = True

    def join(self, name):
        self.joined.append(name)
        return True

    async def stop(self):
        self.stopped = True


@pytest.fixture
def launch(monkeypatch):
    """Service factory with a recording IRC and the bridge signals captured."""
    _LaunchIrc.built = []
    monkeypatch.setattr(service_module, "TwitchIrc", _LaunchIrc)
    conn, auth = [], []

    def on_auth(*args):
        auth.append(args)

    bridge.connection_state.connect(conn.append)
    bridge.auth_state.connect(on_auth)

    def make(store=None):
        store = store if store is not None else DictStore(
            {TOKEN_KEY: TwitchTokens(**tokens_json(login="saveduser")).to_json()})
        return make_service({"twitch": {"client_id": "cid"}}, store), store

    yield make, conn, auth
    bridge.connection_state.disconnect(conn.append)
    bridge.auth_state.disconnect(on_auth)


def _validate_ok(login="testuser"):
    return httpx.Response(200, json={
        "client_id": "cid", "login": login, "user_id": "999",
        "scopes": ["chat:read", "chat:edit"], "expires_in": 3600})


def _mock_helix_globals():
    respx.get(f"{BASE}/chat/badges/global").mock(return_value=httpx.Response(200, json={"data": []}))
    respx.get(f"{BASE}/chat/emotes/global").mock(return_value=httpx.Response(200, json={"data": []}))


def _unreachable_events(conn):
    return [e for e in conn if e["state"] == "unreachable"]


@respx.mock
async def test_launch_refreshes_expired_token_before_connect(launch):
    """TSR-6"""
    make, conn, _ = launch
    service, store = make()
    _mock_helix_globals()
    respx.get(VALIDATE_URL).mock(side_effect=lambda req: (
        httpx.Response(401) if req.headers["Authorization"] == "OAuth acc-1" else _validate_ok()))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "acc-2", "refresh_token": "ref-2", "expires_in": 3600,
        "scope": ["chat:read", "chat:edit"]}))
    try:
        await service.start()
    finally:
        await service.stop()
    irc = _LaunchIrc.built[-1]
    assert irc.kwargs["token"] == "acc-2"
    assert irc.kwargs["nick"] == "testuser"
    assert irc.started
    assert not service.unreachable
    assert _unreachable_events(conn) == []


@respx.mock
async def test_launch_offline_connects_with_saved_login(launch):
    """TSR-7"""
    make, conn, auth = launch
    service, store = make()
    respx.get(VALIDATE_URL).mock(side_effect=httpx.ConnectError("offline"))
    try:
        await service.start()  # must not raise
    finally:
        await service.stop()
    irc = _LaunchIrc.built[-1]
    assert irc.kwargs["nick"] == "saveduser"
    assert irc.kwargs["token"] == "acc-1"
    assert irc.started
    assert service.helix is None
    assert service._check_pending is True
    assert service.unreachable is True
    assert len(_unreachable_events(conn)) == 1
    assert _unreachable_events(conn)[0] == {
        "platform": "twitch", "channel_id": "", "state": "unreachable", "detail": ""}
    assert store.get(TOKEN_KEY) is not None  # login kept
    assert auth == []


@respx.mock
async def test_launch_slow_check_connects_after_10s(launch, monkeypatch):
    """TSR-8 (the 10 s limit is patched small)"""
    make, conn, _ = launch
    monkeypatch.setattr(service_module, "LAUNCH_CHECK_TIMEOUT", 0.05)
    service, store = make()

    async def never_answers(request):
        await asyncio.sleep(30)

    respx.get(VALIDATE_URL).mock(side_effect=never_answers)
    try:
        await asyncio.wait_for(service.start(), 5)
    finally:
        await service.stop()
    irc = _LaunchIrc.built[-1]
    assert irc.started
    assert irc.kwargs["nick"] == "saveduser"
    assert irc.kwargs["token"] == "acc-1"
    assert service._check_pending is True
    assert len(_unreachable_events(conn)) == 1


@respx.mock
async def test_launch_timeout_lets_refresh_finish_and_save(launch, monkeypatch):
    """Review focus 1: the launch stops waiting at the limit but never cancels
    the in-flight refresh - single-use refresh tokens would be lost."""
    make, _, _ = launch
    monkeypatch.setattr(service_module, "LAUNCH_CHECK_TIMEOUT", 0.05)
    service, store = make()
    respx.get(VALIDATE_URL).mock(side_effect=lambda req: (
        httpx.Response(401) if req.headers["Authorization"] == "OAuth acc-1" else _validate_ok()))

    async def slow_refresh(request):
        await asyncio.sleep(0.4)  # answers well after the launch limit
        return httpx.Response(200, json={
            "access_token": "acc-2", "refresh_token": "ref-2", "expires_in": 3600,
            "scope": ["chat:read", "chat:edit"]})

    respx.post(TOKEN_URL).mock(side_effect=slow_refresh)
    try:
        await service.start()
        irc = _LaunchIrc.built[-1]
        assert irc.started
        assert irc.kwargs["token"] == "acc-1"  # connected with the saved login
        assert TwitchTokens.from_json(store.get(TOKEN_KEY)).access_token == "acc-1"
        async with asyncio.timeout(5):
            while TwitchTokens.from_json(store.get(TOKEN_KEY)).access_token != "acc-2":
                await asyncio.sleep(0.02)
    finally:
        await service.stop()
    saved = TwitchTokens.from_json(store.get(TOKEN_KEY))
    assert saved.refresh_token == "ref-2"
    assert TOKEN_KEY not in store.deleted


@respx.mock
async def test_launch_5xx_keeps_login_and_connects(launch):
    """TSR-9"""
    make, conn, auth = launch
    service, store = make()
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(503))
    try:
        await service.start()
    finally:
        await service.stop()
    irc = _LaunchIrc.built[-1]
    assert irc.kwargs["nick"] == "saveduser"
    assert irc.kwargs["token"] == "acc-1"
    assert irc.started
    assert store.get(TOKEN_KEY) is not None
    assert TOKEN_KEY not in store.deleted
    assert service.unreachable is True
    assert auth == []


@respx.mock
async def test_launch_rejected_login_goes_read_only(launch):
    """TSR-21"""
    make, _, auth = launch
    service, store = make()
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400))
    try:
        await service.start()
    finally:
        await service.stop()
    irc = _LaunchIrc.built[-1]
    assert irc.is_anonymous
    assert irc.kwargs["nick"] is None
    assert irc.started
    assert store.get(TOKEN_KEY) is None
    assert TOKEN_KEY in store.deleted
    assert service.tokens is None
    assert service.helix is None
    assert ("twitch", False, EXPIRED_MESSAGE) in auth
    assert not service.unreachable


@respx.mock
async def test_launch_without_saved_login_is_silently_anonymous(launch):
    make, conn, auth = launch
    service, _ = make(DictStore())
    try:
        await service.start()
    finally:
        await service.stop()
    irc = _LaunchIrc.built[-1]
    assert irc.is_anonymous
    assert irc.started
    assert auth == []  # nothing was rejected: there was no login
    assert conn == []


async def test_irc_reports_reach_state_to_the_bridge(launch):
    """The callbacks _make_irc hands TwitchIrc drive `unreachable` and the
    connection_state events."""
    make, conn, _ = launch
    service, _ = make(DictStore())
    service.tokens = TwitchTokens(**tokens_json(login="saveduser"))

    async def no_catch_up():  # the welcome's catch-up has its own tests
        pass

    service._catch_up = no_catch_up
    try:
        signed_in = service._make_irc()
        anonymous_service, _ = make(DictStore())
        anonymous = anonymous_service._make_irc()
        signed_in.kwargs["on_connect_failed"]()
        assert service.unreachable
        signed_in.kwargs["on_connected"]()
        assert not service.unreachable
        anonymous.kwargs["on_connected"]()
    finally:
        await service.stop()
        await anonymous_service.stop()
    states = [(e["state"], e["detail"]) for e in conn]
    assert states == [("unreachable", ""), ("connected", "signed_in"), ("connected", "anonymous")]


# -- catch-up on a signed-in welcome ----------------------------------------------------------


@pytest.fixture
def catch_up_env(launch, monkeypatch):
    """A launched-online service with channel metadata routes whose answers
    the test controls; third-party emote loads are recorded, not fetched."""
    make, conn, auth = launch
    state = {"badges": 200, "users": 200, "emote_loads": []}

    def users(request):
        if state["users"] == "offline":
            raise httpx.ConnectError("offline")
        if state["users"] != 200:
            return httpx.Response(state["users"])
        return httpx.Response(200, json={"data": [{"id": "77", "login": "chan"}]})

    def channel_badges(request):
        if state["badges"] != 200:
            return httpx.Response(state["badges"])
        return httpx.Response(200, json={"data": []})

    routes = {
        "users": respx.get(f"{BASE}/users").mock(side_effect=users),
        "badges": respx.get(f"{BASE}/chat/badges", params={"broadcaster_id": "77"}).mock(
            side_effect=channel_badges),
        "emotes": respx.get(f"{BASE}/chat/emotes", params={"broadcaster_id": "77"}).mock(
            return_value=httpx.Response(200, json={"data": []})),
        "global_badges": respx.get(f"{BASE}/chat/badges/global").mock(
            return_value=httpx.Response(200, json={"data": []})),
        "global_emotes": respx.get(f"{BASE}/chat/emotes/global").mock(
            return_value=httpx.Response(200, json={"data": []})),
        "validate": respx.get(VALIDATE_URL).mock(return_value=_validate_ok()),
    }

    def make_service_(store=None):
        service, store = make(store)

        async def load_channel(name, broadcaster_id):
            state["emote_loads"].append(name)

        monkeypatch.setattr(service.registry, "load_channel", load_channel)
        return service, store

    return make_service_, routes, state, conn, auth


async def _until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def _welcome(service):
    """Simulate IRC's signed-in 001 and wait for the catch-up it starts."""
    _LaunchIrc.built[-1].kwargs["on_connected"]()
    task = service._catch_up_task
    if task is not None:
        await asyncio.wait_for(task, 5)


@respx.mock
async def test_catch_up_after_offline_launch(catch_up_env):
    """TSR-12"""
    make, routes, state, _, _ = catch_up_env
    service, store = make()
    routes["validate"].mock(side_effect=httpx.ConnectError("offline"))
    await service.join_channel("chan")
    try:
        await service.start()
        assert service.helix is None
        assert routes["global_badges"].call_count == 0
        calls_while_offline = routes["validate"].call_count

        routes["validate"].mock(side_effect=None, return_value=_validate_ok())  # network is back
        await _welcome(service)
        await _until(lambda: routes["emotes"].call_count == 1)
    finally:
        await service.stop()
    assert routes["validate"].call_count > calls_while_offline
    assert service._check_pending is False
    assert service.tokens.login == "testuser"
    assert service.helix is not None
    assert routes["global_badges"].call_count == 1
    assert routes["global_emotes"].call_count == 1
    assert routes["users"].call_count == 1  # the channel opened before launch loaded too
    assert state["emote_loads"] == ["chan"]


@respx.mock
async def test_catch_up_reloads_failed_channel(catch_up_env):
    """TSR-13"""
    make, routes, state, _, _ = catch_up_env
    service, _ = make()
    state["badges"] = 503
    await service.join_channel("chan")
    try:
        await service.start()
        await _until(lambda: routes["badges"].call_count == 1 and "chan" not in service._metadata_loaded)
        assert routes["emotes"].call_count == 0

        state["badges"] = 200  # Twitch is back
        await _welcome(service)
        await _until(lambda: routes["emotes"].call_count == 1)
    finally:
        await service.stop()
    assert routes["badges"].call_count == 2
    assert "chan" in service._metadata_loaded


@respx.mock
async def test_catch_up_leaves_loaded_channels(catch_up_env):
    """TSR-14"""
    make, routes, state, _, _ = catch_up_env
    service, _ = make()
    await service.join_channel("chan")
    try:
        await service.start()
        await _until(lambda: routes["emotes"].call_count == 1 and state["emote_loads"])
        users_before = routes["users"].call_count
        badges_before = routes["badges"].call_count
        globals_before = routes["global_badges"].call_count

        await _welcome(service)
        await _welcome(service)
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await service.stop()
    assert routes["users"].call_count == users_before
    assert routes["badges"].call_count == badges_before
    assert routes["emotes"].call_count == 1
    assert routes["global_badges"].call_count == globals_before
    assert state["emote_loads"] == ["chan"]


@respx.mock
async def test_channel_opened_during_outage_loads_on_welcome(catch_up_env):
    """TSR-22"""
    make, routes, state, _, _ = catch_up_env
    service, _ = make()
    try:
        await service.start()
        state["users"] = "offline"
        assert await service.join_channel("chan")
        await _until(lambda: routes["users"].call_count == 1 and "chan" not in service._metadata_loaded)
        assert routes["emotes"].call_count == 0

        state["users"] = 200
        await _welcome(service)
        await _until(lambda: routes["emotes"].call_count == 1)
    finally:
        await service.stop()
    assert routes["users"].call_count == 2
    assert state["emote_loads"] == ["chan"]


@respx.mock
async def test_anonymous_welcome_runs_no_catch_up(catch_up_env):
    make, routes, _, _, _ = catch_up_env
    service, _ = make(DictStore())
    await service.join_channel("chan")
    try:
        await service.start()
        await _welcome(service)
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await service.stop()
    assert service._catch_up_task is None
    assert service.helix is None
    assert routes["validate"].call_count == 0
    assert routes["users"].call_count == 0
    assert routes["global_badges"].call_count == 0


@respx.mock
async def test_catch_up_rejected_login_goes_read_only(catch_up_env):
    """A check that was pending at launch ends in a definite rejection (e.g.
    the background refresh got a 400): the welcome's catch-up falls back."""
    make, routes, _, _, auth = catch_up_env
    service, store = make()
    routes["validate"].mock(side_effect=httpx.ConnectError("offline"))
    try:
        await service.start()
        assert service._check_pending is True

        routes["validate"].mock(side_effect=None, return_value=httpx.Response(401))
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(400))
        await _welcome(service)
    finally:
        await service.stop()
    assert ("twitch", False, EXPIRED_MESSAGE) in auth
    assert store.get(TOKEN_KEY) is None
    assert TOKEN_KEY in store.deleted
    assert service.tokens is None
    assert service.helix is None
    assert service._check_pending is False
    assert _LaunchIrc.built[-1].is_anonymous


@respx.mock
async def test_catch_up_unreachable_stays_pending(catch_up_env):
    make, routes, _, _, auth = catch_up_env
    service, store = make()
    routes["validate"].mock(side_effect=httpx.ConnectError("offline"))
    try:
        await service.start()
        await _welcome(service)  # chat connected but Helix/validate still down
        assert service._check_pending is True
        assert service.helix is None

        routes["validate"].mock(side_effect=None, return_value=_validate_ok())
        await _welcome(service)
    finally:
        await service.stop()
    assert service._check_pending is False
    assert service.helix is not None
    assert auth == []
    assert store.get(TOKEN_KEY) is not None


@respx.mock
async def test_catch_up_runs_once_at_a_time(catch_up_env):
    make, routes, _, _, _ = catch_up_env
    service, _ = make()
    release = asyncio.Event()
    started = []

    async def slow_catch_up():
        started.append(1)
        await release.wait()

    service._catch_up = slow_catch_up
    try:
        await service.start()
        on_connected = _LaunchIrc.built[-1].kwargs["on_connected"]
        on_connected()
        on_connected()
        await asyncio.sleep(0)
        assert started == [1]
        release.set()
        await service._catch_up_task
        on_connected()  # the previous run finished: a new one may start
        await service._catch_up_task
    finally:
        await service.stop()
    assert started == [1, 1]


# -- re-login on an IRC auth refusal ------------------------------------------------------------


class _AuthServer(FakeIrcServer):
    """A fake IRC server that welcomes only the tokens in `accepted` (and
    anonymous connections). Any other login gets Twitch's auth NOTICE and a
    close. `attempts` lists the token of every connection, in order."""

    def __init__(self, accepted=()):
        super().__init__()
        self.accepted = set(accepted)
        self.attempts: list[str | None] = []

    async def _handle(self, ws) -> None:
        self.connections.append(ws)
        self.connected.set()
        token = None
        try:
            async for raw in ws:
                line = str(raw)
                self.received.append(line)
                if line.startswith("PASS oauth:"):
                    token = line.removeprefix("PASS oauth:")
                elif line.startswith("NICK"):
                    self.attempts.append(token)
                    if token is None or token in self.accepted:
                        await ws.send(f":tmi.twitch.tv 001 {line[5:]} :Welcome, GLHF!")
                    else:
                        await ws.send(":tmi.twitch.tv NOTICE * :Login authentication failed")
                        await ws.close()
        except Exception:
            pass
        finally:
            if ws in self.connections:
                self.connections.remove(ws)

    def welcomed(self, token) -> bool:
        return token in self.attempts and token in self.accepted


def _refreshed(access, refresh):
    return httpx.Response(200, json={
        "access_token": access, "refresh_token": refresh,
        "expires_in": 3600, "scope": ["chat:read", "chat:edit"]})


@pytest.fixture
async def relogin_env(monkeypatch):
    """A signed-in service on a real TwitchIrc talking to `_AuthServer`.
    Yields (service, server, store, refresh_route, conn, auth, clients)."""
    server = _AuthServer()
    await server.start()
    clients: list[TwitchIrc] = []

    def build(mapper, **kwargs):
        client = TwitchIrc(mapper, ws_url=server.url, backoff_base=0.05, backoff_cap=0.1, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(service_module, "TwitchIrc", build)
    conn, auth = [], []

    def on_auth(*args):
        auth.append(args)

    bridge.connection_state.connect(conn.append)
    bridge.auth_state.connect(on_auth)
    store = DictStore({TOKEN_KEY: TwitchTokens(**tokens_json(login="saveduser")).to_json()})
    service = make_service({"twitch": {"client_id": "cid"}}, store)
    monkeypatch.setattr(service, "_kick_metadata", lambda name: None)
    with respx.mock:
        respx.get(VALIDATE_URL).mock(return_value=_validate_ok())
        _mock_helix_globals()
        refresh_route = respx.post(TOKEN_URL)
        yield service, server, store, refresh_route, conn, auth, clients
    bridge.connection_state.disconnect(conn.append)
    bridge.auth_state.disconnect(on_auth)
    await service.stop()
    await server.stop()


async def test_expired_token_relogin_rejoins(relogin_env):
    """TSR-15"""
    service, server, store, refresh, conn, auth, _ = relogin_env
    server.accepted = {"acc-2"}
    refresh.mock(return_value=_refreshed("acc-2", "ref-2"))
    await service.join_channel("chan")
    await service.start()
    await server.wait_for(lambda ln: ln == "JOIN #chan" and server.welcomed("acc-2"))
    assert refresh.call_count == 1
    assert service.tokens.access_token == "acc-2"
    assert TwitchTokens.from_json(store.get(TOKEN_KEY)).access_token == "acc-2"
    assert not service.irc.is_anonymous
    assert auth == []


async def test_relogin_replaces_connection_once(relogin_env):
    """Review Focus 2: one live client after a refusal + a successful refresh."""
    service, server, _, refresh, _, _, clients = relogin_env
    server.accepted = {"acc-2"}
    refresh.mock(return_value=_refreshed("acc-2", "ref-2"))
    await service.start()
    await _until(lambda: server.welcomed("acc-2") and server.connections)
    await asyncio.sleep(0.4)  # a leftover acc-1 client would keep reconnecting
    settled = len(server.attempts)
    await asyncio.sleep(0.4)
    assert len(server.attempts) == settled
    assert server.attempts[-1] == "acc-2"
    live = [c for c in clients if c._run_task is not None and not c._run_task.done()]
    assert live == [service.irc]
    assert len(server.connections) == 1


async def test_relogin_single_flight(relogin_env):
    """Review Focus 3: refusals during a running re-login start no second refresh."""
    service, server, _, refresh, _, _, _ = relogin_env
    server.accepted = {"acc-2"}
    gate = asyncio.Event()

    posts = []

    async def slow_refresh(request):
        posts.append(1)
        await gate.wait()
        return _refreshed("acc-2", "ref-2")

    refresh.mock(side_effect=slow_refresh)
    relogins = []
    real_relogin = service._relogin

    def counting_relogin(*args):
        relogins.append(args)
        return real_relogin(*args)

    service._relogin = counting_relogin
    await service.start()
    # the acc-1 client keeps reconnecting and being refused while the refresh waits
    await _until(lambda: server.attempts.count("acc-1") >= 3)
    assert posts == [1]
    assert len(relogins) == 1  # later refusals start no second re-login
    gate.set()
    await _until(lambda: server.welcomed("acc-2") and server.connections)
    assert posts == [1]
    assert len(relogins) == 1


async def test_new_token_refused_goes_read_only_keeps_login(relogin_env):
    """TSR-16"""
    service, server, store, refresh, _, auth, _ = relogin_env
    refresh.mock(return_value=_refreshed("acc-2", "ref-2"))  # nothing is accepted
    await service.start()
    await _until(lambda: auth)
    await _until(lambda: service.irc.is_anonymous and server.connections)
    assert auth == [("twitch", False, REJECTED_MESSAGE)]
    assert refresh.call_count == 1
    assert service.tokens is None
    assert store.deleted == []
    assert TwitchTokens.from_json(store.get(TOKEN_KEY)).access_token == "acc-2"
    await asyncio.sleep(0.3)
    assert refresh.call_count == 1


async def test_revoked_login_goes_read_only(relogin_env):
    """TSR-17"""
    service, server, store, refresh, _, auth, _ = relogin_env
    refresh.mock(return_value=httpx.Response(400))
    await service.start()
    await _until(lambda: auth)
    await _until(lambda: service.irc.is_anonymous and server.connections)
    assert auth == [("twitch", False, EXPIRED_MESSAGE)]
    assert TOKEN_KEY in store.deleted
    assert store.get(TOKEN_KEY) is None
    assert service.tokens is None


async def test_refresh_unreachable_retries_at_next_refusal(relogin_env):
    """TSR-18"""
    service, server, store, refresh, conn, auth, _ = relogin_env
    server.accepted = {"acc-2"}
    refresh.mock(side_effect=[httpx.Response(503), _refreshed("acc-2", "ref-2")])
    await service.start()
    await _until(lambda: refresh.call_count >= 1)
    await _until(lambda: _unreachable_events(conn))
    assert service.unreachable
    assert TwitchTokens.from_json(store.get(TOKEN_KEY)).access_token == "acc-1"
    assert store.deleted == []
    assert auth == []
    # the still-running client reconnects, is refused again, and the refresh retries
    await _until(lambda: server.welcomed("acc-2") and server.connections)
    assert refresh.call_count == 2
    assert service.tokens.access_token == "acc-2"
    assert not service.unreachable


async def test_later_expiry_refreshes_again(relogin_env):
    """TSR-19"""
    service, server, store, refresh, _, auth, _ = relogin_env
    server.accepted = {"acc-2"}
    refresh.mock(side_effect=[_refreshed("acc-2", "ref-2"), _refreshed("acc-3", "ref-3")])
    await service.start()
    await _until(lambda: server.welcomed("acc-2") and server.connections)
    await _until(lambda: not service._relogin_spent)  # the welcome cleared it
    server.accepted = {"acc-3"}  # acc-2 expires later
    await server.close_all()
    await _until(lambda: server.attempts.count("acc-3") >= 1 and server.connections)
    assert refresh.call_count == 2
    assert service.tokens.access_token == "acc-3"
    assert auth == []


@respx.mock
async def test_fall_back_read_only_replaces_connection_and_rejoins(launch):
    make, _, auth = launch
    service, _ = make()
    respx.get(VALIDATE_URL).mock(return_value=_validate_ok())
    _mock_helix_globals()
    await service.join_channel("chan")
    await service.start()
    old = _LaunchIrc.built[-1]
    await service._fall_back_read_only(REJECTED_MESSAGE, delete=False)
    new = _LaunchIrc.built[-1]
    assert old.stopped
    assert new is not old and new.started and new.is_anonymous
    assert new.joined == ["chan"]
    assert service.irc is new
    assert auth == [("twitch", False, REJECTED_MESSAGE)]


@respx.mock
async def test_stale_and_anonymous_refusals_are_ignored(launch):
    make, _, auth = launch
    service, store = make()
    respx.get(VALIDATE_URL).mock(return_value=_validate_ok())
    _mock_helix_globals()
    refresh = respx.post(TOKEN_URL).mock(return_value=_refreshed("acc-2", "ref-2"))
    await service.start()
    signed_in = _LaunchIrc.built[-1]
    await service.disconnect()  # swaps in an anonymous client
    anonymous = _LaunchIrc.built[-1]
    refusal = AuthFailed("Login authentication failed")
    signed_in.kwargs["on_auth"](refusal)  # stale: no longer service.irc
    anonymous.kwargs["on_auth"](refusal)  # anonymous: nothing to refresh
    await asyncio.sleep(0.05)
    assert refresh.call_count == 0
    assert service._relogin_task is None
    assert auth == []


# -- account features during an outage ----------------------------------------------------------


def _expired_validate(request):
    """Validate answers 401 for the saved token (it needs a refresh)."""
    return httpx.Response(401)


@respx.mock
async def test_ban_during_outage_fails_only_that_action(launch):
    """TSR-20"""
    make, _, auth = launch
    service, store = make()
    service.tokens = service.auth.load_tokens()
    service.helix = Helix("cid", service._user_token)
    respx.get(VALIDATE_URL).mock(side_effect=_expired_validate)
    refresh = respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    ban = respx.post(f"{BASE}/moderation/bans").mock(return_value=httpx.Response(200, json={"data": []}))
    before = store.get(TOKEN_KEY)
    try:
        with pytest.raises(TwitchUnreachable):
            await service.helix.ban_user("1", "2", "3")
        assert refresh.call_count == 1
        # Only that action failed: login kept, tokens untouched, no read-only fallback.
        assert store.get(TOKEN_KEY) == before
        assert store.deleted == []
        assert service.tokens is not None and service.tokens.access_token == "acc-1"
        assert service._expired_task is None
        assert auth == []
        # The next action asks Twitch again (the refresh answers this time).
        refresh.mock(return_value=_refreshed("acc-2", "ref-2"))
        respx.get(VALIDATE_URL).mock(side_effect=lambda req: (
            httpx.Response(401) if req.headers["Authorization"] == "OAuth acc-1" else _validate_ok()))
        await service.helix.ban_user("1", "2", "3")
        assert refresh.call_count == 2
        assert ban.call_count == 1
    finally:
        await service.stop()


@respx.mock
async def test_helix_definite_rejection_falls_back(launch):
    """Review Focus 4 / TSR-4"""
    make, _, auth = launch
    service, store = make()
    respx.get(VALIDATE_URL).mock(return_value=_validate_ok())
    _mock_helix_globals()
    await service.start()
    signed_in = _LaunchIrc.built[-1]
    assert not signed_in.is_anonymous
    # Concurrent Helix calls all see the rejection; only one fall-back runs.
    respx.get(VALIDATE_URL).mock(side_effect=_expired_validate)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400))
    helix = service.helix
    helix.invalidate_token()  # the cached access token has lapsed
    try:
        results = await asyncio.gather(
            helix.user_by_login("a"), helix.user_by_login("b"), return_exceptions=True)
        assert all(isinstance(r, HelixError) for r in results)
        assert service._expired_task is not None
        await service._expired_task
        read_only = _LaunchIrc.built[-1]
        assert signed_in.stopped
        assert read_only is not signed_in and read_only.is_anonymous and read_only.started
        assert service.irc is read_only
        assert service.tokens is None
        assert service.helix is None
        assert store.get(TOKEN_KEY) is None
        assert auth == [("twitch", False, EXPIRED_MESSAGE)]
        assert len(_LaunchIrc.built) == 2
    finally:
        await service.stop()


@respx.mock
async def test_reconnect_unreachable_keeps_connection(launch):
    make, _, auth = launch
    service, store = make()
    respx.get(VALIDATE_URL).mock(return_value=_validate_ok())
    _mock_helix_globals()
    await service.start()
    current = service.irc
    respx.get(VALIDATE_URL).mock(side_effect=_expired_validate)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    try:
        assert await service.reconnect() is False
        assert service.irc is current
        assert not current.stopped
        assert len(_LaunchIrc.built) == 1
    finally:
        await service.stop()
    assert store.get(TOKEN_KEY) is not None
    assert auth == [("twitch", False,
                     "sign-in succeeded but the token refresh failed — try connecting again")]


@respx.mock
async def test_sign_out_cancels_running_relogin(launch):
    make, _, auth = launch
    service, store = make()
    respx.get(VALIDATE_URL).mock(return_value=_validate_ok())
    _mock_helix_globals()
    await service.start()
    signed_in = _LaunchIrc.built[-1]
    release, asked = asyncio.Event(), asyncio.Event()

    async def slow_refresh(request):
        asked.set()
        await release.wait()
        return _refreshed("acc-2", "ref-2")

    respx.post(TOKEN_URL).mock(side_effect=slow_refresh)
    signed_in.kwargs["on_auth"](AuthFailed("Login authentication failed"))
    await asyncio.wait_for(asked.wait(), 5)  # the re-login waits on the slow refresh
    relogin = service._relogin_task
    service._relogin_spent = True
    try:
        await service.disconnect()
        assert relogin.done()
        assert service._relogin_task is None
        assert service._relogin_spent is False
        anonymous = _LaunchIrc.built[-1]
        assert anonymous.is_anonymous and service.irc is anonymous
        release.set()
        await asyncio.sleep(0.1)  # the finished refresh must not swap the connection back
        assert service.irc is anonymous
        assert len(_LaunchIrc.built) == 2
        assert service.tokens is None
        assert auth == []
    finally:
        release.set()
        await service.stop()


@respx.mock
async def test_connect_account_resets_spent_relogin(launch):
    make, _, _ = launch
    service, store = make()
    respx.get(VALIDATE_URL).mock(return_value=_validate_ok())
    _mock_helix_globals()
    await service.start()
    service._relogin_spent = True
    try:
        assert await service.reconnect() is True
    finally:
        await service.stop()
    assert service._relogin_spent is False
