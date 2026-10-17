"""YouTubeApi: quota accounting, error parsing, endpoint shapes (MockTransport)."""

import json

import httpx
import pytest

from chattersyn.services.youtube.api import (
    QUOTA,
    QuotaTracker,
    YouTubeApi,
    YoutubeApiError,
)


def _client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://www.googleapis.com/youtube/v3")


def _api(handler, token="tok"):
    return YouTubeApi(lambda: _token(token), QuotaTracker(), client=_client(handler))


async def _token(value):
    return value


def _json_response(request, body, status=200):
    return httpx.Response(status, json=body, request=request)


# -- plumbing ---------------------------------------------------------------------------


async def test_successful_get_charges_quota():
    def handler(request):
        return _json_response(request, {"kind": "videoListResponse", "items": []})

    api = _api(handler)
    result = await api.videos("dQw4w9WgXcQ")
    assert result == {"kind": "videoListResponse", "items": []}
    assert api.quota.used == QUOTA["meta"]


async def test_quota_exceeded_marks_tracker_exhausted():
    def handler(request):
        return _json_response(request, {
            "error": {"code": 403, "message": "quota",
                      "errors": [{"reason": "quotaExceeded", "message": "quota"}]},
        }, status=403)

    api = _api(handler)
    with pytest.raises(YoutubeApiError) as exc_info:
        await api.videos("dQw4w9WgXcQ")
    assert exc_info.value.is_quota
    assert api.quota.exhausted() is True


async def test_error_reason_parsed_from_first_error():
    def handler(request):
        return _json_response(request, {
            "error": {"code": 400, "message": "bad",
                      "errors": [{"reason": "messageTextInvalid", "message": "no"}]},
        }, status=400)

    api = _api(handler)
    with pytest.raises(YoutubeApiError) as exc_info:
        await api.live_chat_insert("Cg0KC2Zvbw", "http://x")
    assert exc_info.value.reason == "messageTextInvalid"
    assert exc_info.value.http_status == 400


async def test_rate_limit_is_retryable():
    def handler(request):
        return _json_response(request, {
            "error": {"code": 429, "message": "slow down",
                      "errors": [{"reason": "rateLimitExceeded", "message": "slow"}]},
        }, status=429)

    api = _api(handler)
    with pytest.raises(YoutubeApiError) as exc_info:
        await api.videos("dQw4w9WgXcQ")
    assert exc_info.value.is_rate_limited
    assert exc_info.value.retryable


async def test_missing_token_is_unauthenticated():
    async def no_token():
        return None

    api = YouTubeApi(no_token, QuotaTracker(), client=_client(lambda r: _json_response(r, {})))
    with pytest.raises(YoutubeApiError) as exc_info:
        await api.videos("dQw4w9WgXcQ")
    assert exc_info.value.reason == "unauthenticated"


async def test_5xx_is_retryable():
    def handler(request):
        return httpx.Response(503, request=request)

    api = _api(handler)
    with pytest.raises(YoutubeApiError) as exc_info:
        await api.videos("dQw4w9WgXcQ")
    assert exc_info.value.retryable


# -- endpoints ---------------------------------------------------------------------------


async def test_videos_404_is_none():
    def handler(request):
        return _json_response(request, {"error": {"errors": [{"reason": "notFound"}]}}, status=404)

    api = _api(handler)
    assert await api.videos("dQw4w9WgXcQ") is None


async def test_videos_batch_joins_ids():
    seen = {}

    def handler(request):
        seen["ids"] = request.url.params["id"]
        return _json_response(request, {"items": []})

    api = _api(handler)
    await api.videos_batch(["a" * 11, "b" * 11])
    assert seen["ids"] == f"{'a' * 11},{'b' * 11}"


async def test_channel_by_handle_strips_at():
    seen = {}

    def handler(request):
        seen["handle"] = request.url.params["forHandle"]
        seen["part"] = request.url.params["part"]
        return _json_response(request, {"items": []})

    api = _api(handler)
    await api.channel_by_handle("@forsen")
    assert seen["handle"] == "forsen"
    assert "statistics" in seen["part"]  # usercard needs subscriber counts


async def test_live_chat_insert_shape():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["part"] = request.url.params["part"]
        return _json_response(request, {"id": "inserted-1", "snippet": {}})

    api = _api(handler)
    result = await api.live_chat_insert("Cg0KC2Zvbw", "hello world")
    assert result["id"] == "inserted-1"
    assert seen["part"] == "snippet"
    assert seen["body"]["snippet"]["liveChatId"] == "Cg0KC2Zvbw"
    assert seen["body"]["snippet"]["type"] == "textMessageEvent"
    assert seen["body"]["snippet"]["textMessageDetails"]["messageText"] == "hello world"
    assert api.quota.used == QUOTA["insert"]


async def test_live_chat_list_shape():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return _json_response(request, {"items": [], "pollingIntervalMillis": 8000})

    api = _api(handler)
    await api.live_chat_list("Cg0KC2Zvbw", page_token="p1", max_results=200)
    assert seen["params"]["liveChatId"] == "Cg0KC2Zvbw"
    assert seen["params"]["pageToken"] == "p1"
    assert seen["params"]["maxResults"] == "200"
    assert api.quota.used == QUOTA["list"]


# -- live chat bans (moderation) -------------------------------------------------------


async def test_live_chat_ban_permanent_shape():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["params"] = dict(request.url.params)
        seen["body"] = json.loads(request.content)
        return _json_response(request, {"id": "ban-123"})

    api = _api(handler)
    resp = await api.live_chat_ban("lc1", "UCbad")
    assert resp == {"id": "ban-123"}
    assert api.quota.used == QUOTA["ban"]
    assert seen["method"] == "POST"
    assert seen["params"] == {"part": "snippet"}
    assert seen["body"] == {"snippet": {
        "liveChatId": "lc1", "type": "permanent",
        "bannedUserDetails": {"channelId": "UCbad"}}}


async def test_live_chat_ban_temporary_has_duration():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _json_response(request, {"id": "ban-4"})

    api = _api(handler)
    resp = await api.live_chat_ban("lc1", "UCbad", duration=300)
    assert resp == {"id": "ban-4"}
    snippet = seen["body"]["snippet"]
    assert snippet["type"] == "temporary"
    assert snippet["banDurationSeconds"] == 300


async def test_live_chat_unban_delete_shape():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["params"] = dict(request.url.params)
        return httpx.Response(204, request=request)

    api = _api(handler)
    resp = await api.live_chat_unban("ban-123")
    assert resp == {}
    assert api.quota.used == QUOTA["ban"]
    assert seen["method"] == "DELETE"
    assert seen["params"] == {"id": "ban-123"}


async def test_quota_tracker_daily_rollover():
    tracker = QuotaTracker()
    tracker.charge(5)
    assert tracker.used == 5
    tracker.mark_exhausted()
    assert tracker.exhausted()
    tracker._day = None  # simulate a new day
    assert not tracker.exhausted()
    assert tracker.used == 0
