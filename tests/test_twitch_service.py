"""TwitchService: client-id resolution, Helix token provider, token sync."""

import asyncio

import httpx
import pytest
import respx

from chattersyn.config.settings import Settings
from chattersyn.core.bus import bridge
from chattersyn.services.twitch import service as service_module
from chattersyn.services.twitch.auth import TOKEN_KEY, TOKEN_URL, VALIDATE_URL, TwitchTokens
from chattersyn.services.twitch.helix import BASE
from chattersyn.services.twitch.service import EXPIRED_MESSAGE, TwitchService


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
