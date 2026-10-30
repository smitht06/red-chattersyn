"""YouTubeAuth: loopback capture flow, exchange/persist, expiry clearing."""

import asyncio
import json
import urllib.parse

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from chattersyn.services.youtube import auth as auth_module
from chattersyn.services.youtube.auth import TOKEN_KEY, YouTubeAuth, YouTubeAuthError


class DictStore:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


CLIENT_SECRETS = {
    "installed": {
        "client_id": "cid.apps.googleusercontent.com",
        "project_id": "p",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "sekrit",
        "redirect_uris": ["http://localhost"],
    }
}


class FakeFlow:
    """Stand-in for google_auth_oauthlib.flow.InstalledAppFlow."""

    last_config: dict = {}
    fetch_calls: list[dict] = []

    @classmethod
    def from_client_config(cls, config, scopes, redirect_uri=None):
        cls.last_config = {"config": config, "scopes": scopes, "redirect_uri": redirect_uri}
        return cls()

    def authorization_url(self, prompt=None, access_type=None):
        assert prompt == "consent"  # forces a refresh token every time
        assert access_type == "offline"
        return "https://accounts.example/authorize?x=1", "state-abc"

    def fetch_token(self, code=None):
        FakeFlow.fetch_calls.append({"code": code})
        self.credentials = _creds("acc-1")


def _creds(access_token, refresh_token="ref-1"):
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cid",
        client_secret="sekrit",
    )


def _token_blob(access_token="acc-old", refresh_token="ref-old",
                expiry="2020-01-01T00:00:00Z"):
    # from_authorized_user_info treats a missing expiry as expired.
    return json.dumps({
        "token": access_token, "refresh_token": refresh_token,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid", "client_secret": "sekrit",
        "expiry": expiry,
    })


@pytest.fixture
def fake_flow(monkeypatch):
    import google_auth_oauthlib.flow as flow_module

    FakeFlow.last_config = {}
    FakeFlow.fetch_calls = []
    monkeypatch.setattr(flow_module, "InstalledAppFlow", FakeFlow)
    yield FakeFlow


async def browser_get(url: str) -> str:
    """Simulate the browser redirect over a raw socket (respx-free)."""
    parsed = urllib.parse.urlparse(url)
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
    writer.write(
        f"GET {parsed.path}?{parsed.query} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data.decode("latin-1", "replace")


# -- client secrets -------------------------------------------------------------------


def test_set_client_secrets_validates_shape():
    auth = YouTubeAuth(None, DictStore())
    auth.set_client_secrets(CLIENT_SECRETS)
    assert auth.configured()


def test_set_client_secrets_rejects_garbage():
    auth = YouTubeAuth(None, DictStore())
    with pytest.raises(YouTubeAuthError):
        auth.set_client_secrets({"hello": "world"})
    with pytest.raises(YouTubeAuthError):
        auth.set_client_secrets({"installed": {"client_id": "x"}})  # no secret


def test_configured_loads_stored_blob():
    store = DictStore({"youtube_client_secrets": json.dumps(CLIENT_SECRETS)})
    assert YouTubeAuth(None, store).configured()


@pytest.fixture
def builtin_client(monkeypatch):
    monkeypatch.setattr(auth_module, "BUILTIN_CLIENT_ID", "builtin.apps.googleusercontent.com")
    monkeypatch.setattr(auth_module, "BUILTIN_CLIENT_SECRET", "builtin-secret")


@pytest.fixture
def no_builtin_client(monkeypatch):
    monkeypatch.setattr(auth_module, "BUILTIN_CLIENT_ID", "")
    monkeypatch.setattr(auth_module, "BUILTIN_CLIENT_SECRET", "")


async def test_builtin_client_needs_no_setup(fake_flow, builtin_client):
    auth = YouTubeAuth(None, DictStore())
    assert auth.configured() and auth.uses_builtin_client()
    await auth.begin_flow()
    installed = fake_flow.last_config["config"]["installed"]
    assert installed["client_id"] == "builtin.apps.googleusercontent.com"
    assert installed["client_secret"] == "builtin-secret"
    await auth.close()


async def test_own_client_secrets_win_over_the_builtin_client(fake_flow, builtin_client):
    auth = YouTubeAuth(None, DictStore())
    auth.set_client_secrets(CLIENT_SECRETS)
    assert not auth.uses_builtin_client()
    await auth.begin_flow()
    assert fake_flow.last_config["config"] == CLIENT_SECRETS
    await auth.close()


def test_stored_own_client_wins_after_restart(builtin_client):
    store = DictStore({"youtube_client_secrets": json.dumps(CLIENT_SECRETS)})
    auth = YouTubeAuth(None, store)
    assert auth.configured() and not auth.uses_builtin_client()


def test_nothing_configured_without_any_client(no_builtin_client):
    auth = YouTubeAuth(None, DictStore())
    assert not auth.configured()
    assert not auth.uses_builtin_client()


# -- loopback flow ----------------------------------------------------------------------


async def test_begin_flow_binds_loopback_port(fake_flow):
    auth = YouTubeAuth(None, DictStore())
    auth.set_client_secrets(CLIENT_SECRETS)
    url = await auth.begin_flow()
    assert url.startswith("https://accounts.example/")
    redirect = FakeFlow.last_config["redirect_uri"]
    assert redirect.startswith("http://localhost:")
    port = int(redirect.rsplit(":", 1)[1])
    assert port > 0
    assert FakeFlow.last_config["scopes"] == ["https://www.googleapis.com/auth/youtube.force-ssl"]
    await auth.close()


async def test_full_flow_exchanges_and_persists(fake_flow):
    store = DictStore()
    auth = YouTubeAuth(None, store)
    auth.set_client_secrets(CLIENT_SECRETS)
    await auth.begin_flow()
    port = int(FakeFlow.last_config["redirect_uri"].rsplit(":", 1)[1])

    body = await browser_get(f"http://localhost:{port}/?code=code-123&state=state-abc")
    assert "connected" in body  # OK_PAGE

    ok = await auth.wait_for_code(timeout=2.0)
    assert ok
    assert FakeFlow.fetch_calls == [{"code": "code-123"}]
    assert TOKEN_KEY in store.data
    assert auth.token_info()["valid"]


async def test_wrong_state_rejected(fake_flow):
    store = DictStore()
    auth = YouTubeAuth(None, store)
    auth.set_client_secrets(CLIENT_SECRETS)
    await auth.begin_flow()
    port = int(FakeFlow.last_config["redirect_uri"].rsplit(":", 1)[1])

    body = await browser_get(f"http://localhost:{port}/?code=code-123&state=WRONG")
    assert "failed" in body.lower()

    assert not await auth.wait_for_code(timeout=2.0)
    assert TOKEN_KEY not in store.data
    assert not FakeFlow.fetch_calls


async def test_wait_for_code_times_out(fake_flow):
    auth = YouTubeAuth(None, DictStore())
    auth.set_client_secrets(CLIENT_SECRETS)
    await auth.begin_flow()
    assert not await auth.wait_for_code(timeout=0.1)
    assert auth._server is None  # server closed in finally


async def test_begin_flow_without_secrets_raises(no_builtin_client):
    with pytest.raises(YouTubeAuthError):
        await YouTubeAuth(None, DictStore()).begin_flow()


# -- token lifecycle ---------------------------------------------------------------------


async def test_ensure_valid_refreshes_expired(monkeypatch):
    store = DictStore({TOKEN_KEY: _token_blob()})
    auth = YouTubeAuth(None, store)
    creds = Credentials.from_authorized_user_info(json.loads(store.data[TOKEN_KEY]))
    refreshed = []

    def fake_refresh(request):
        refreshed.append(request)
        creds.token = "acc-new"

    monkeypatch.setattr(creds, "refresh", fake_refresh)
    monkeypatch.setattr(Credentials, "from_authorized_user_info",
                        staticmethod(lambda info: creds))
    result = await auth.ensure_valid()
    assert result is creds
    assert refreshed  # a refresh attempt was made
    assert json.loads(store.data[TOKEN_KEY])["token"] == "acc-new"


async def test_ensure_valid_clears_on_invalid_grant(monkeypatch):
    store = DictStore({TOKEN_KEY: _token_blob()})
    auth = YouTubeAuth(None, store)
    creds = Credentials.from_authorized_user_info(json.loads(store.data[TOKEN_KEY]))

    def fake_refresh(request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(creds, "refresh", fake_refresh)
    monkeypatch.setattr(Credentials, "from_authorized_user_info",
                        staticmethod(lambda info: creds))

    notices = []
    from chattersyn.core.bus import bridge
    connection = bridge.auth_state.connect(
        lambda p, ok, detail: notices.append((p, ok, detail)))
    try:
        result = await auth.ensure_valid()
    finally:
        bridge.auth_state.disconnect(connection)
    assert result is None
    assert TOKEN_KEY not in store.data  # cleared: 7-day testing expiry path
    assert notices and notices[0][0] == "youtube" and not notices[0][1]
    assert "7 days" in notices[0][2]


async def test_ensure_valid_skips_refresh_when_token_is_fresh(monkeypatch):
    store = DictStore({TOKEN_KEY: _token_blob(expiry="2030-01-01T00:00:00Z")})
    auth = YouTubeAuth(None, store)
    creds = Credentials.from_authorized_user_info(json.loads(store.data[TOKEN_KEY]))
    refreshed = []

    def fake_refresh(request):
        refreshed.append(request)

    monkeypatch.setattr(creds, "refresh", fake_refresh)
    monkeypatch.setattr(Credentials, "from_authorized_user_info",
                        staticmethod(lambda info: creds))
    result = await auth.ensure_valid()
    assert result is creds
    assert not refreshed  # no refresh churn on every call


async def test_ensure_valid_keeps_grant_on_retryable_refresh_error(monkeypatch):
    store = DictStore({TOKEN_KEY: _token_blob()})
    auth = YouTubeAuth(None, store)
    creds = Credentials.from_authorized_user_info(json.loads(store.data[TOKEN_KEY]))

    def fake_refresh(request):
        raise RefreshError("Connection error.", retryable=True)

    monkeypatch.setattr(creds, "refresh", fake_refresh)
    monkeypatch.setattr(Credentials, "from_authorized_user_info",
                        staticmethod(lambda info: creds))
    result = await auth.ensure_valid()
    assert result is None
    assert TOKEN_KEY in store.data  # a valid grant survives a transient error


async def test_begin_flow_twice_raises(fake_flow):
    auth = YouTubeAuth(None, DictStore())
    auth.set_client_secrets(CLIENT_SECRETS)
    await auth.begin_flow()
    try:
        with pytest.raises(YouTubeAuthError):
            await auth.begin_flow()
    finally:
        await auth.close()


async def test_wait_for_code_exchange_failure_reports(fake_flow, monkeypatch):
    from chattersyn.core.bus import bridge

    store = DictStore()
    auth = YouTubeAuth(None, store)
    auth.set_client_secrets(CLIENT_SECRETS)
    await auth.begin_flow()
    port = int(FakeFlow.last_config["redirect_uri"].rsplit(":", 1)[1])

    def boom(code=None):
        raise RuntimeError("fetch exploded")

    monkeypatch.setattr(auth._flow, "fetch_token", boom)
    notices = []
    connection = bridge.auth_state.connect(
        lambda p, ok, detail: notices.append((p, ok, detail)))
    try:
        await browser_get(f"http://localhost:{port}/?code=code-123&state=state-abc")
        ok = await auth.wait_for_code(timeout=2.0)
    finally:
        bridge.auth_state.disconnect(connection)
    assert not ok
    assert TOKEN_KEY not in store.data
    assert notices and notices[0][0] == "youtube" and not notices[0][1]


async def test_clear_token():
    store = DictStore({TOKEN_KEY: _token_blob()})
    auth = YouTubeAuth(None, store)
    auth.clear_token()
    assert TOKEN_KEY not in store.data
