"""TwitchAuth: PKCE loopback flow, exchange, validate/refresh, clear on invalid."""

import asyncio
import base64
import hashlib
import urllib.parse

import httpx
import pytest
import respx

from chattersyn.services.twitch.auth import (
    AUTHORIZE_URL,
    TOKEN_KEY,
    TOKEN_URL,
    VALIDATE_URL,
    TwitchAuth,
    TwitchAuthError,
    TwitchTokens,
    TwitchUnreachable,
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
    auth = TwitchAuth("cid", store, ports=ports)
    return auth


def s256(value: str) -> str:
    """base64url(SHA256(value)) without padding — the PKCE S256 challenge."""
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


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
    params = refresh_route.calls[0].request.url.params
    assert params["grant_type"] == "refresh_token"
    assert params["refresh_token"] == "ref-1"
    assert params["client_id"] == "cid"
    assert "client_secret" not in params  # public client — no secret anywhere
    assert validate_route.call_count == 2


@respx.mock
async def test_ensure_valid_clears_when_refresh_rejected():
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
async def test_refresh_accepts_scope_as_array():
    # The token endpoint returns scope as a JSON array (the real shape);
    # refresh must normalize it like the exchange does.
    # (The second validate omits "scopes" so the refreshed list survives.)
    store = DictStore({TOKEN_KEY: TwitchTokens(**tokens_json()).to_json()})
    respx.get(VALIDATE_URL).mock(side_effect=[httpx.Response(401), httpx.Response(
        200, json={"client_id": "cid", "login": "testuser", "user_id": "999"})])
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "acc-3", "refresh_token": "ref-3",
                   "scope": ["chat:read"], "expires_in": 3600}))
    auth = make_auth(store)
    try:
        tokens = await auth.ensure_valid()
    finally:
        await auth.close()
    assert tokens is not None
    assert tokens.scopes == ["chat:read"]


@respx.mock
async def test_ensure_valid_none_without_stored_tokens():
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200))
    auth = make_auth(DictStore())
    try:
        assert await auth.ensure_valid() is None
    finally:
        await auth.close()


# -- interactive flow -------------------------------------------------------------------


async def test_begin_flow_includes_pkce_and_state():
    auth = make_auth(DictStore(), ports=(0,))
    try:
        url = await auth.begin_flow()
    finally:
        await auth.close()
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["client_id"] == ["cid"]
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [s256(auth._code_verifier)]
    assert params["state"] == [auth._state]
    assert auth._state != ""
    assert "client_secret" not in params


async def test_sign_in_requests_follow_scope():
    """TUF-9: Connect Twitch asks for moderator:read:followers too."""
    auth = make_auth(DictStore(), ports=(0,))
    try:
        url = await auth.begin_flow()
    finally:
        await auth.close()
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["scope"][0].split() == [
        "chat:read", "chat:edit", "moderator:manage:banned_users", "moderator:read:followers"]


@respx.mock
async def test_full_loopback_flow_exchanges_and_saves():
    store = DictStore()
    exchange_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "acc-9", "refresh_token": "ref-9",
                       # Twitch returns scope as a JSON array in the token
                       # response — the real shape this flow must survive.
                       "scope": ["chat:read", "chat:edit"], "expires_in": 3600}
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

        authz = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        state = authz["state"][0]
        verifier = auth._code_verifier
        assert authz["code_challenge"] == [s256(verifier)]
        redirect = authz["redirect_uri"][0]
        port = int(redirect.rsplit(":", 1)[1])
        waiter = asyncio.ensure_future(auth.wait_for_code())

        body = await browser_get(
            f"http://127.0.0.1:{port}/?code=auth-code-1&scope=chat%3Aread&state={state}")
        assert "ChatterSyn" in body

        tokens = await waiter
        assert tokens is not None
        assert tokens.access_token == "acc-9"
        assert tokens.login == "testuser"
        # exchange got the code + redirect_uri + PKCE verifier, no secret
        params = exchange_route.calls[0].request.url.params
        assert params["code"] == "auth-code-1"
        assert params["grant_type"] == "authorization_code"
        assert params["redirect_uri"].startswith("http://localhost:")
        assert params["code_verifier"] == verifier
        assert params["client_id"] == "cid"
        assert "client_secret" not in params
        # PKCE verifier scrubbed after the exchange
        assert auth._code_verifier == ""
        assert auth._state == ""
        # tokens persisted
        assert TOKEN_KEY in store.data
    finally:
        await auth.close()


@respx.mock
async def test_state_mismatch_rejects_code_and_skips_exchange():
    store = DictStore()
    exchange_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "acc-x", "expires_in": 3600})
    )
    auth = make_auth(store, ports=(0,))
    try:
        await auth.begin_flow()
        port = int(auth._redirect_uri.rsplit(":", 1)[1])
        waiter = asyncio.ensure_future(auth.wait_for_code())
        body = await browser_get(f"http://127.0.0.1:{port}/?code=stolen-code&state=wrong")
        assert "failed" in body
        assert await waiter is None
        assert exchange_route.call_count == 0
        assert TOKEN_KEY not in store.data
        assert auth._code_verifier == ""  # flow secrets scrubbed
        assert auth._state == ""
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
    auth = TwitchAuth("", DictStore(), ports=(0,))
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
        await browser_get(f"http://127.0.0.1:{port}/?code=bad&state={auth._state}")
        try:
            await waiter
            raise AssertionError("expected TwitchAuthError")
        except TwitchAuthError:
            pass
        assert TOKEN_KEY not in store.data
    finally:
        await auth.close()


@respx.mock
async def test_exchange_missing_client_secret_hints_public_client():
    # A confidential Twitch app can't do PKCE — the error must say what to fix.
    store = DictStore()
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        400, json={"status": 400, "message": "missing client secret"}))
    auth = make_auth(store, ports=(0,))
    try:
        await auth.begin_flow()
        port = int(auth._redirect_uri.rsplit(":", 1)[1])
        waiter = asyncio.ensure_future(auth.wait_for_code())
        await browser_get(f"http://127.0.0.1:{port}/?code=bad&state={auth._state}")
        try:
            await waiter
            raise AssertionError("expected TwitchAuthError")
        except TwitchAuthError as exc:
            assert "Client type to Public" in str(exc)
        assert TOKEN_KEY not in store.data
    finally:
        await auth.close()


@respx.mock
async def test_ensure_valid_syncs_scopes_from_validate():
    """Validate reports what the token grants today — a token minted before
    the moderation scope was requested must not claim to have it."""
    stored = TwitchTokens(**tokens_json())
    stored.scopes = ["chat:read", "chat:edit", "moderator:manage:banned_users"]
    store = DictStore({TOKEN_KEY: stored.to_json()})
    respx.get(VALIDATE_URL).mock(return_value=VALIDATE_OK)
    auth = make_auth(store)
    try:
        tokens = await auth.ensure_valid()
    finally:
        await auth.close()
    assert tokens.scopes == ["chat:read", "chat:edit"]
    assert TwitchTokens.from_json(store.get(TOKEN_KEY)).scopes == ["chat:read", "chat:edit"]


# -- outcome classification: keep the login unless Twitch rejects it -----------------------

REFRESH_OK = httpx.Response(
    200,
    json={"access_token": "acc-2", "refresh_token": "ref-2", "scope": "chat:read chat:edit", "expires_in": 3600},
)


def stored_store(**overrides):
    return DictStore({TOKEN_KEY: TwitchTokens(**tokens_json(**overrides)).to_json()})


async def _ensure_unreachable(store):
    auth = make_auth(store)
    try:
        with pytest.raises(TwitchUnreachable):
            await auth.ensure_valid()
    finally:
        await auth.close()


@respx.mock
async def test_refresh_5xx_keeps_login():
    store = stored_store()
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    await _ensure_unreachable(store)
    assert store.deleted == []
    assert store.get(TOKEN_KEY) is not None


@respx.mock
async def test_refresh_network_error_keeps_login():
    store = stored_store()
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    await _ensure_unreachable(store)
    assert store.deleted == []


@respx.mock
async def test_refresh_timeout_keeps_login():
    store = stored_store()
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    await _ensure_unreachable(store)
    assert store.deleted == []


@respx.mock
async def test_refresh_429_keeps_login():
    store = stored_store()
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(429))
    await _ensure_unreachable(store)
    assert store.deleted == []


@pytest.mark.parametrize("status", [400, 401])
@respx.mock
async def test_refresh_rejection_deletes_login(status):
    store = stored_store()
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(status))
    auth = make_auth(store)
    try:
        assert await auth.ensure_valid() is None
    finally:
        await auth.close()
    assert TOKEN_KEY in store.deleted
    assert store.get(TOKEN_KEY) is None


@respx.mock
async def test_expired_without_refresh_token_deletes_login():
    store = stored_store(refresh_token="")
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
    refresh_route = respx.post(TOKEN_URL)
    auth = make_auth(store)
    try:
        assert await auth.ensure_valid() is None
    finally:
        await auth.close()
    assert TOKEN_KEY in store.deleted
    assert not refresh_route.called


@respx.mock
async def test_valid_without_refresh_token_kept():
    store = stored_store(refresh_token="")
    respx.get(VALIDATE_URL).mock(return_value=VALIDATE_OK)
    auth = make_auth(store)
    try:
        tokens = await auth.ensure_valid()
    finally:
        await auth.close()
    assert tokens is not None and tokens.access_token == "acc-1"
    assert store.deleted == []


@pytest.mark.parametrize(
    "mock",
    [
        {"return_value": httpx.Response(500)},
        {"return_value": httpx.Response(503)},
        {"return_value": httpx.Response(429)},
        {"side_effect": httpx.ConnectError("down")},
        {"side_effect": httpx.ReadTimeout("slow")},
    ],
)
@respx.mock
async def test_validate_5xx_and_network_raise_unreachable(mock):
    respx.get(VALIDATE_URL).mock(**mock)
    auth = make_auth(DictStore())
    try:
        with pytest.raises(TwitchUnreachable):
            await auth.validate("acc-1")
    finally:
        await auth.close()


@respx.mock
async def test_validate_unreachable_keeps_login():
    store = stored_store()
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(502))
    await _ensure_unreachable(store)
    assert store.deleted == []


def _validate_by_token(request):
    token = request.headers["Authorization"].removeprefix("OAuth ")
    return httpx.Response(401) if token == "acc-1" else VALIDATE_OK


@respx.mock
async def test_concurrent_ensure_valid_one_refresh():
    store = stored_store()
    respx.get(VALIDATE_URL).mock(side_effect=_validate_by_token)
    refresh_route = respx.post(TOKEN_URL).mock(return_value=REFRESH_OK)
    auth = make_auth(store)
    try:
        first, second = await asyncio.gather(auth.ensure_valid(), auth.ensure_valid())
    finally:
        await auth.close()
    assert refresh_route.call_count == 1
    assert first is not None and second is not None
    assert first.access_token == second.access_token == "acc-2"


@respx.mock
async def test_rejected_token_forces_refresh():
    store = stored_store()
    validate_route = respx.get(VALIDATE_URL).mock(return_value=VALIDATE_OK)
    refresh_route = respx.post(TOKEN_URL).mock(return_value=REFRESH_OK)
    auth = make_auth(store)
    try:
        tokens = await auth.ensure_valid(rejected_token="acc-1")
    finally:
        await auth.close()
    assert refresh_route.call_count == 1
    assert tokens is not None and tokens.access_token == "acc-2"
    # the refused token was never validated: only the fresh one was
    assert [c.request.headers["Authorization"] for c in validate_route.calls] == ["OAuth acc-2"]


@respx.mock
async def test_rejected_token_without_refresh_token_deletes_login():
    store = stored_store(refresh_token="")
    respx.get(VALIDATE_URL).mock(return_value=VALIDATE_OK)
    refresh_route = respx.post(TOKEN_URL)
    auth = make_auth(store)
    try:
        assert await auth.ensure_valid(rejected_token="acc-1") is None
    finally:
        await auth.close()
    assert TOKEN_KEY in store.deleted
    assert not refresh_route.called


@respx.mock
async def test_rejected_token_already_replaced_skips_refresh():
    store = stored_store(access_token="acc-2", refresh_token="ref-2")
    respx.get(VALIDATE_URL).mock(return_value=VALIDATE_OK)
    refresh_route = respx.post(TOKEN_URL)
    auth = make_auth(store)
    try:
        tokens = await auth.ensure_valid(rejected_token="acc-1")
    finally:
        await auth.close()
    assert not refresh_route.called
    assert tokens is not None and tokens.access_token == "acc-2"


@respx.mock
async def test_cancelled_caller_does_not_cancel_refresh():
    store = stored_store()
    respx.get(VALIDATE_URL).mock(side_effect=_validate_by_token)
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_refresh(request):
        started.set()
        await release.wait()
        return REFRESH_OK

    refresh_route = respx.post(TOKEN_URL).mock(side_effect=slow_refresh)
    auth = make_auth(store)
    try:
        waiter = asyncio.ensure_future(asyncio.wait_for(auth.ensure_valid(), timeout=0.2))
        await started.wait()
        with pytest.raises(TimeoutError):
            await waiter
        # the caller stopped waiting; the refresh is still in flight
        assert TwitchTokens.from_json(store.get(TOKEN_KEY)).access_token == "acc-1"
        release.set()
        for _ in range(50):
            if TwitchTokens.from_json(store.get(TOKEN_KEY)).access_token == "acc-2":
                break
            await asyncio.sleep(0.02)
        saved = TwitchTokens.from_json(store.get(TOKEN_KEY))
        assert saved.access_token == "acc-2" and saved.refresh_token == "ref-2"
        # a later check is not wedged by the cancelled waiter and does not refresh again
        tokens = await asyncio.wait_for(auth.ensure_valid(), timeout=2)
        assert tokens is not None and tokens.access_token == "acc-2"
        assert refresh_route.call_count == 1
    finally:
        release.set()
        await auth.close()
    assert store.deleted == []


@respx.mock
async def test_check_during_inflight_refresh_reuses_it():
    """A new check arriving after the first waiter gave up joins the running
    refresh instead of spending the single-use refresh token a second time."""
    store = stored_store()
    respx.get(VALIDATE_URL).mock(side_effect=_validate_by_token)
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_refresh(request):
        started.set()
        await release.wait()
        return REFRESH_OK

    refresh_route = respx.post(TOKEN_URL).mock(side_effect=slow_refresh)
    auth = make_auth(store)
    try:
        waiter = asyncio.ensure_future(asyncio.wait_for(auth.ensure_valid(), timeout=0.1))
        await started.wait()
        with pytest.raises(TimeoutError):
            await waiter
        second = asyncio.ensure_future(auth.ensure_valid(rejected_token="acc-1"))
        await asyncio.sleep(0.05)
        release.set()
        tokens = await asyncio.wait_for(second, timeout=2)
    finally:
        release.set()
        await auth.close()
    assert refresh_route.call_count == 1
    assert tokens is not None and tokens.access_token == "acc-2"


@respx.mock
async def test_refresh_finishing_during_another_check_keeps_new_login():
    """Caller A gives up waiting (launch limit); B loads the old tokens and is
    mid-validate when A's background refresh saves the new ones. B's 401 must
    not spend the already-used refresh token (Twitch answers 400) and delete
    the good login."""
    store = stored_store()
    release_refresh = asyncio.Event()
    refresh_started = asyncio.Event()
    b_validating = asyncio.Event()
    release_b_validate = asyncio.Event()
    spent: set[str] = set()

    async def validate(request):
        token = request.headers["Authorization"].removeprefix("OAuth ")
        if token != "acc-1":
            return VALIDATE_OK
        if refresh_started.is_set():  # caller B: slow answer for the old token
            b_validating.set()
            await release_b_validate.wait()
        await asyncio.sleep(0)
        return httpx.Response(401)

    async def refresh(request):
        refresh_token = request.url.params["refresh_token"]
        if refresh_token in spent:
            return httpx.Response(400)  # single-use refresh token
        spent.add(refresh_token)
        refresh_started.set()
        await release_refresh.wait()
        return REFRESH_OK

    respx.get(VALIDATE_URL).mock(side_effect=validate)
    refresh_route = respx.post(TOKEN_URL).mock(side_effect=refresh)
    auth = make_auth(store)
    try:
        a = asyncio.ensure_future(asyncio.wait_for(auth.ensure_valid(), timeout=0.1))
        await refresh_started.wait()
        with pytest.raises(TimeoutError):
            await a
        b = asyncio.ensure_future(auth.ensure_valid())
        await b_validating.wait()
        release_refresh.set()
        for _ in range(100):
            if TwitchTokens.from_json(store.get(TOKEN_KEY)).access_token == "acc-2":
                break
            await asyncio.sleep(0.01)
        release_b_validate.set()
        tokens = await asyncio.wait_for(b, timeout=2)
    finally:
        release_refresh.set()
        release_b_validate.set()
        await auth.close()
    assert refresh_route.call_count == 1
    assert store.deleted == []
    assert tokens is not None and tokens.access_token == "acc-2"
    assert TwitchTokens.from_json(store.get(TOKEN_KEY)).access_token == "acc-2"


# -- final review: odd answers count as unreachable ----------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "http://portal.example/"}),
        httpx.Response(403),
        httpx.Response(407),
        httpx.Response(200, text="<html>hotel login</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
@respx.mock
async def test_validate_odd_answers_raise_unreachable(response):
    respx.get(VALIDATE_URL).mock(return_value=response)
    store = stored_store()
    await _ensure_unreachable(store)
    assert store.deleted == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"refresh_token": "ref-2", "expires_in": 3600}),
        httpx.Response(200, json={"access_token": "", "refresh_token": "ref-2"}),
        httpx.Response(200, text="<html>portal</html>"),
        httpx.Response(302, headers={"location": "http://portal.example/"}),
        httpx.Response(403),
    ],
)
@respx.mock
async def test_refresh_odd_answers_keep_login(response):
    store = stored_store()
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
    respx.post(TOKEN_URL).mock(return_value=response)
    await _ensure_unreachable(store)
    assert store.deleted == []
    assert TwitchTokens.from_json(store.get(TOKEN_KEY)).access_token == "acc-1"


@pytest.mark.parametrize(
    ("route", "mock"),
    [
        ("validate", {"side_effect": httpx.ConnectError("[Errno -2] Name or service not known")}),
        ("validate", {"return_value": httpx.Response(503)}),
        ("refresh", {"side_effect": httpx.ReadTimeout("read timed out")}),
        ("refresh", {"return_value": httpx.Response(500)}),
    ],
)
@respx.mock
async def test_unreachable_message_is_plain_text(route, mock):
    store = stored_store()
    if route == "validate":
        respx.get(VALIDATE_URL).mock(**mock)
    else:
        respx.get(VALIDATE_URL).mock(return_value=httpx.Response(401))
        respx.post(TOKEN_URL).mock(**mock)
    auth = make_auth(store)
    try:
        with pytest.raises(TwitchUnreachable) as info:
            await auth.ensure_valid()
    finally:
        await auth.close()
    assert str(info.value) == "can't reach Twitch"


# -- final review: in-flight token work never writes over a sign-out or a new login ----


@respx.mock
async def test_refresh_finishing_after_sign_out_saves_nothing():
    store = stored_store()
    respx.get(VALIDATE_URL).mock(side_effect=_validate_by_token)
    release, started = asyncio.Event(), asyncio.Event()

    async def slow_refresh(request):
        started.set()
        await release.wait()
        return REFRESH_OK

    respx.post(TOKEN_URL).mock(side_effect=slow_refresh)
    auth = make_auth(store)
    try:
        check = asyncio.ensure_future(auth.ensure_valid())
        await started.wait()
        auth.clear()  # sign-out while the refresh is in flight
        release.set()
        assert await asyncio.wait_for(check, 2) is None
        await asyncio.sleep(0.05)
    finally:
        release.set()
        await auth.close()
    assert TOKEN_KEY not in store.data
    assert store.deleted == [TOKEN_KEY]  # only the sign-out's delete


@respx.mock
async def test_validate_finishing_after_sign_out_saves_nothing():
    store = stored_store()
    release, asked = asyncio.Event(), asyncio.Event()

    async def slow_validate(request):
        asked.set()
        await release.wait()
        return VALIDATE_OK

    respx.get(VALIDATE_URL).mock(side_effect=slow_validate)
    auth = make_auth(store)
    try:
        check = asyncio.ensure_future(auth.ensure_valid())
        await asked.wait()
        auth.clear()
        release.set()
        assert await asyncio.wait_for(check, 2) is None
    finally:
        release.set()
        await auth.close()
    assert TOKEN_KEY not in store.data
    assert store.deleted == [TOKEN_KEY]


@respx.mock
async def test_validate_finishing_after_store_changed_rechecks():
    store = stored_store()
    release, asked = asyncio.Event(), asyncio.Event()

    async def validate(request):
        token = request.headers["Authorization"].removeprefix("OAuth ")
        if token == "acc-1":
            asked.set()
            await release.wait()
            return VALIDATE_OK
        return httpx.Response(200, json={"login": "bob", "user_id": "42", "scopes": ["chat:read"]})

    respx.get(VALIDATE_URL).mock(side_effect=validate)
    auth = make_auth(store)
    try:
        check = asyncio.ensure_future(auth.ensure_valid())
        await asked.wait()
        store.set(TOKEN_KEY, TwitchTokens(**tokens_json(access_token="acc-b", refresh_token="ref-b")).to_json())
        release.set()
        tokens = await asyncio.wait_for(check, 2)
    finally:
        release.set()
        await auth.close()
    assert tokens is not None and tokens.access_token == "acc-b" and tokens.login == "bob"
    saved = TwitchTokens.from_json(store.get(TOKEN_KEY))
    assert saved.access_token == "acc-b" and saved.login == "bob"


@respx.mock
async def test_refresh_for_old_account_does_not_overwrite_new_login():
    """A refresh for account A is in flight while the user completes Connect
    as account B: A's late answer must not overwrite B."""
    store = stored_store()
    release, refreshing = asyncio.Event(), asyncio.Event()

    def validate(request):
        token = request.headers["Authorization"].removeprefix("OAuth ")
        if token == "acc-1":
            return httpx.Response(401)
        if token == "acc-b":
            return httpx.Response(200, json={"login": "bob", "user_id": "42", "scopes": ["chat:read"]})
        return VALIDATE_OK

    async def token_endpoint(request):
        if request.url.params["grant_type"] == "refresh_token":
            refreshing.set()
            await release.wait()
            return REFRESH_OK  # account A's new tokens (acc-2)
        return httpx.Response(200, json={
            "access_token": "acc-b", "refresh_token": "ref-b", "scope": ["chat:read"], "expires_in": 3600})

    respx.get(VALIDATE_URL).mock(side_effect=validate)
    respx.post(TOKEN_URL).mock(side_effect=token_endpoint)
    auth = make_auth(store, ports=(0,))
    try:
        check = asyncio.ensure_future(auth.ensure_valid())
        await refreshing.wait()
        url = await auth.begin_flow()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        port = int(query["redirect_uri"][0].rsplit(":", 1)[1])
        flow = asyncio.ensure_future(auth.wait_for_code())
        await browser_get(f"http://127.0.0.1:{port}/?code=c&state={query['state'][0]}")
        assert (await flow).access_token == "acc-b"
        release.set()
        tokens = await asyncio.wait_for(check, 2)
        await asyncio.sleep(0.05)
    finally:
        release.set()
        await auth.close()
    saved = TwitchTokens.from_json(store.get(TOKEN_KEY))
    assert saved.access_token == "acc-b" and saved.login == "bob"
    assert tokens is not None and tokens.access_token == "acc-b"
    assert store.deleted == []
