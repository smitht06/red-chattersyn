"""Per-call credential choice: the user's API key pays for public reads, the
Google sign-in for everything else (YQB-21..27, 43..45)."""

import httpx
import pytest

from chattersyn.config.settings import Settings
from chattersyn.services.youtube.api import YouTubeApi, YoutubeApiError
from chattersyn.services.youtube.quota import Pool, QuotaTracker
from chattersyn.services.youtube.service import YouTubeService

BASE = "https://www.googleapis.com/youtube/v3"


class Store:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


def _error(reason, status=400, details=False):
    body = {"code": status, "message": reason}
    if details:
        body["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    else:
        body["errors"] = [{"reason": reason, "message": reason}]
    return {"error": body}


def _make(responses, *, token="tok", key="KEY"):
    """Api over a MockTransport; `responses` is a list of (status, json) used in
    order (the last repeats). Returns (api, requests)."""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        status, body = responses[min(len(seen), len(responses)) - 1]
        return httpx.Response(status, json=body, request=request)

    async def get_token():
        return token

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE)
    api = YouTubeApi(get_token, QuotaTracker(), get_key=lambda: key, client=client)
    return api, seen


OK = (200, {"items": []})


def _uses_key(request):
    return request.url.params.get("key") == "KEY" and "authorization" not in request.headers


def _uses_bearer(request):
    return request.headers.get("authorization") == "Bearer tok" and "key" not in request.url.params


async def test_key_only_read_uses_key():
    """Through the real wiring: an API key with no sign-in must read."""
    store = Store()
    store.set("youtube_api_key", "KEY")
    svc = YouTubeService(Settings({}), store)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"items": []}, request=request)

    svc.api._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE)
    try:
        await svc.api.videos("vid")
    finally:
        await svc.stop()
    assert _uses_key(seen[0])


async def test_yqb21_public_read_prefers_key():
    api, seen = _make([OK])
    await api.channel_by_handle("@x")
    assert _uses_key(seen[0])
    assert api.quota.used_in(Pool.KEY) == 1
    assert api.quota.used == 0


async def test_yqb23_live_chat_insert_uses_bearer():
    api, seen = _make([(200, {"id": "m"})])
    await api.live_chat_insert("chat", "hi")
    assert _uses_bearer(seen[0])
    assert api.quota.used == 20


async def test_yqb25_no_key_reads_use_bearer():
    api, seen = _make([OK], key=None)
    await api.channel_by_handle("@x")
    assert _uses_bearer(seen[0])


async def test_signin_forces_bearer_for_videos():
    api, seen = _make([OK])
    await api.videos("v", signin=True)
    assert _uses_bearer(seen[0])


@pytest.mark.parametrize("details", [False, True])
async def test_yqb26_invalid_key_retries_with_signin(details):
    api, seen = _make([(400, _error("keyInvalid" if not details else "API_KEY_INVALID",
                                    details=details)), OK, OK])
    await api.channel_by_handle("@x")
    assert _uses_key(seen[0]) and _uses_bearer(seen[1])
    assert api.key_problem
    await api.playlist_items("pl")
    assert len(seen) == 3 and _uses_bearer(seen[2])


async def test_live_bad_key_reply_shape_is_a_key_problem():
    # What YouTube really sends for a bad key: errors[].reason is only
    # "badRequest"; the key reason sits in the ErrorInfo detail.
    body = {"error": {
        "code": 400, "message": "API key not valid. Please pass a valid API key.",
        "errors": [{"message": "API key not valid. Please pass a valid API key.",
                    "domain": "global", "reason": "badRequest"}],
        "status": "INVALID_ARGUMENT",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": "API_KEY_INVALID", "domain": "googleapis.com",
                     "metadata": {"service": "youtube.googleapis.com"}}],
    }}
    api, seen = _make([(400, body), OK])
    await api.channel_by_handle("@x")
    assert api.key_problem
    assert _uses_key(seen[0]) and _uses_bearer(seen[1])


async def test_yqb43_invalid_key_without_token_is_unauthenticated():
    api, seen = _make([(400, _error("keyInvalid"))], token=None)
    with pytest.raises(YoutubeApiError) as exc_info:
        await api.videos("v")
    assert exc_info.value.is_unauthenticated
    assert api.key_problem
    assert len(seen) == 1


async def test_yqb44_reset_key_problem_uses_key_again():
    api, seen = _make([(400, _error("keyInvalid")), OK, OK])
    await api.videos_batch(["a"])
    api.reset_key_problem()
    assert api.key_problem == ""
    await api.videos_batch(["a"])
    assert _uses_key(seen[2])


async def test_yqb45_rate_limit_is_retryable_and_not_a_key_problem():
    api, seen = _make([(403, _error("rateLimitExceeded", 403))])
    with pytest.raises(YoutubeApiError) as exc_info:
        await api.videos("v")
    assert exc_info.value.retryable
    assert api.key_problem == ""
    assert len(seen) == 1


async def test_yqb27_key_quota_exceeded_marks_key_pool_only():
    api, seen = _make([(403, _error("quotaExceeded", 403))])
    with pytest.raises(YoutubeApiError) as exc_info:
        await api.videos("v")
    assert exc_info.value.is_quota
    assert api.quota.exhausted(Pool.KEY)
    assert not api.quota.exhausted(Pool.SHARED)
    assert len(seen) == 1  # no Bearer retry
    with pytest.raises(YoutubeApiError) as again:
        await api.channel_by_id("UC1")
    assert again.value.is_quota
    assert len(seen) == 1  # no request sent
    assert await api.read_pool() is Pool.KEY
    assert await api.read_pool(signin=True) is Pool.SHARED


async def test_api_key_problem_texts():
    store = Store()
    store.set("youtube_api_key", "KEY")
    svc = YouTubeService(Settings({}), store)
    try:
        assert svc.api_key_problem() == ""
        svc.api.key_problem = "keyInvalid"
        assert svc.api_key_problem() == "API key not working — YouTube reading paused"
        svc.tokens = object()
        assert svc.api_key_problem() == "API key not working — using your Google sign-in"
        svc.set_api_key("NEW")
        assert svc.api_key_problem() == ""
    finally:
        await svc.stop()
