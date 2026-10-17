"""TwitchAuth: loopback flow, exchange, validate/refresh, clear on invalid."""

import asyncio
import urllib.parse

import httpx
import respx

from chattersyn.services.twitch.auth import (
    AUTHORIZE_URL,
    TOKEN_KEY,
    TOKEN_URL,
    VALIDATE_URL,
    TwitchAuth,
    TwitchAuthError,
    TwitchTokens,
)


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


VALIDATE_OK = httpx.Response(200, json={"client_id": "cid", "login": "testuser", "user_id": "999", "scopes": ["chat:read", "chat:edit"], "expires_in": 3600})


def make_auth(store, ports=(0,)):
    auth = TwitchAuth("cid", "secret", store, ports=ports)
    return auth


async def browser_get(url: str) -> str:
    """Simulate the browser redirect over a raw socket — respx intercepts
    every httpx request inside the test, so httpx cannot be used here."""
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


# -- ensure_valid ----------------------------------------------------------------------


@respx.mock
async def test_ensure_valid_returns_stored_and_updates_identity():
    store = DictStore({TOKEN_KEY: TwitchTokens(**tokens_json()).to_json()})
    respx.get(VALIDATE_URL).mock(return_value=VALIDATE_OK)
    auth = make_auth(store)
    try:
        tokens = await auth.ensure_valid()
    finally:
        await auth.close()
    assert tokens is not None
    assert tokens.login == "testuser"
    assert tokens.user_id == "999"
    # identity written back to the store
    saved = TwitchTokens.from_json(store.get(TOKEN_KEY))
    assert saved is not None and saved.login == "testuser"


@respx.mock
async def test_ensure_valid_refreshes_on_401():
    store = DictStore({TOKEN_KEY: TwitchTokens(**tokens_json()).to_json()})
    validate_route = respx.get(VALIDATE_URL).mock(
        side_effect=[httpx.Response(401), VALIDATE_OK]
    )
    refresh_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "acc-2", "refresh_token": "ref-2", "scope": "chat:read chat:edit", "expires_in": 3600}
        )
    )
    auth = make_auth(store)
    try:
        tokens = await auth.ensure_valid()
    finally:
        await auth.close()
    assert tokens is not None
    assert tokens.access_token == "acc-2" and tokens.refresh_token == "ref-2"
    assert refresh_route.calls[0].request.url.params["grant_type"] == "refresh_token"
    assert refresh_route.calls[0].request.url.params["refresh_token"] == "ref-1"
    assert validate_route.call_count == 2


@respx.mock
async def test_ensure_valid_clears_when_refresh_fails():
    store = DictStore({TOKEN_KEY: TwitchTokens(**tokens_json()).to_json()})
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400))
    auth = make_auth(store)
    try:
        assert await auth.ensure_valid() is None
    finally:
        await auth.close()
    assert TOKEN_KEY in store.deleted
    assert store.get(TOKEN_KEY) is None


@respx.mock
async def test_ensure_valid_none_without_stored_tokens():
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200))
    auth = make_auth(DictStore())
    try:
        assert await auth.ensure_valid() is None
    finally:
        await auth.close()


# -- interactive flow -------------------------------------------------------------------


@respx.mock
async def test_full_loopback_flow_exchanges_and_saves():
    store = DictStore()
    exchange_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "acc-9", "refresh_token": "ref-9", "scope": "chat:read chat:edit", "expires_in": 3600}
        )
    )
    respx.get(VALIDATE_URL).mock(return_value=VALIDATE_OK)
    auth = make_auth(store, ports=(0,))
    try:
        url = await auth.begin_flow()
        assert url.startswith(AUTHORIZE_URL + "?")
        assert "force_verify=true" in url
        assert "redirect_uri=http%3A%2F%2Flocalhost%3A" in url
        assert "chat%3Aread+chat%3Aedit" in url or "chat%3Aread" in url

        redirect = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"][0]
        port = int(redirect.rsplit(":", 1)[1])
        waiter = asyncio.ensure_future(auth.wait_for_code())

        body = await browser_get(f"http://127.0.0.1:{port}/?code=auth-code-1&scope=chat%3Aread")
        assert "ChatterSyn" in body

        tokens = await waiter
        assert tokens is not None
        assert tokens.access_token == "acc-9"
        assert tokens.login == "testuser"
        # exchange got the code + redirect_uri
        params = exchange_route.calls[0].request.url.params
        assert params["code"] == "auth-code-1"
        assert params["grant_type"] == "authorization_code"
        assert params["redirect_uri"].startswith("http://localhost:")
        # tokens persisted
        assert TOKEN_KEY in store.data
    finally:
        await auth.close()


@respx.mock
async def test_flow_timeout_returns_none():
    auth = make_auth(DictStore(), ports=(0,))
    try:
        await auth.begin_flow()
        assert await auth.wait_for_code(timeout=0.1) is None
        # server released after the flow ends
        assert auth._server is None
    finally:
        await auth.close()


async def test_flow_without_credentials_raises():
    auth = TwitchAuth("", "", DictStore(), ports=(0,))
    try:
        try:
            await auth.begin_flow()
            raise AssertionError("expected TwitchAuthError")
        except TwitchAuthError:
            pass
    finally:
        await auth.close()


@respx.mock
async def test_exchange_failure_raises_and_clears_nothing():
    store = DictStore()
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, text="bad code"))
    auth = make_auth(store, ports=(0,))
    try:
        await auth.begin_flow()
        port = int(auth._redirect_uri.rsplit(":", 1)[1])
        waiter = asyncio.ensure_future(auth.wait_for_code())
        await browser_get(f"http://127.0.0.1:{port}/?code=bad")
        try:
            await waiter
            raise AssertionError("expected TwitchAuthError")
        except TwitchAuthError:
            pass
        assert TOKEN_KEY not in store.data
    finally:
        await auth.close()
