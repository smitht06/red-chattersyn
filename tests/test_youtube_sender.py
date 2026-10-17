"""YoutubeSender: validation, bucket pacing, error mapping."""


import pytest

from chattersyn.services.youtube.api import QuotaTracker, YouTubeApi, YoutubeApiError
from chattersyn.services.youtube.sender import YoutubeSender, YoutubeSendError, _map_error


def _sender(handler, **kwargs):
    api = YouTubeApi(lambda: _token(), QuotaTracker(), client=_client(handler))
    kwargs.setdefault("capacity", 100)  # avoid waiting on the bucket in tests
    kwargs.setdefault("spacing", 0.0)
    return YoutubeSender(api, **kwargs)


def _client(handler):
    import httpx
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://www.googleapis.com/youtube/v3")


async def _token():
    return "tok"


# -- validation -----------------------------------------------------------------------


def test_validate_ok():
    assert _sender(lambda r: None).validate("hello") is None


def test_validate_too_long():
    error = _sender(lambda r: None).validate("x" * 201)
    assert error is not None
    assert error.reason == "too_long"


def test_validate_url_blocked():
    error = _sender(lambda r: None).validate("check https://example.com")
    assert error is not None
    assert error.reason == "url_blocked"


def test_validate_url_allowed_when_configured_off():
    assert _sender(lambda r: None, block_urls=False).validate("check https://example.com") is None


def test_validate_exact_limit_ok():
    assert _sender(lambda r: None).validate("x" * 200) is None


# -- send + error mapping -------------------------------------------------------------


async def test_send_posts_and_returns_inserted_resource():
    async def handler(request):
        import httpx
        return httpx.Response(200, json={"id": "inserted-1"}, request=request)

    result = await _sender(handler).send("Cg0KC2Zvbw", "hello")
    assert result["id"] == "inserted-1"


async def test_send_validation_failure_raises_before_http():
    called = False

    async def handler(request):
        nonlocal called
        called = True
        import httpx
        return httpx.Response(200, json={"id": "x"}, request=request)

    with pytest.raises(YoutubeSendError) as exc_info:
        await _sender(handler).send("Cg0KC2Zvbw", "https://example.com")
    assert exc_info.value.reason == "url_blocked"
    assert not called


async def test_send_maps_message_invalid():
    async def handler(request):
        import httpx
        return httpx.Response(400, json={
            "error": {"code": 400, "message": "bad",
                      "errors": [{"reason": "messageTextInvalid", "message": "no"}]},
        }, request=request)

    with pytest.raises(YoutubeSendError) as exc_info:
        await _sender(handler).send("Cg0KC2Zvbw", "<b>hi</b>")
    assert exc_info.value.reason == "message_invalid"
    assert not exc_info.value.retryable


@pytest.mark.parametrize("reason,expected", [
    ("quotaExceeded", "quota_exhausted"),
    ("rateLimitExceeded", "rate_limited"),
    ("liveChatEnded", "chat_ended"),
    ("liveChatDisabled", "chat_disabled"),
    ("liveChatNotFound", "chat_not_found"),
    ("forbidden", "forbidden"),
    ("backendError", "transient"),
])
def test_error_mapping(reason, expected):
    error = _map_error(YoutubeApiError(
        403 if reason not in ("rateLimitExceeded", "backendError") else 429,
        reason, "detail", retryable=reason in ("rateLimitExceeded", "backendError")))
    assert error.reason == expected
    if reason == "rateLimitExceeded":
        assert error.retryable


def test_5xx_transient_is_not_retryable():
    # A 5xx may have committed the non-idempotent insert — retrying could
    # double-post, so only rateLimitExceeded gets retryable=True.
    error = _map_error(YoutubeApiError(503, "backendError", "boom", retryable=True))
    assert error.reason == "transient"
    assert not error.retryable


async def test_bucket_spacing_limits_pace():
    # 3 capacity with 1/s refill: sends must be spaced ~1s apart
    import time

    async def handler(request):
        import httpx
        return httpx.Response(200, json={"id": "x"}, request=request)

    sender = _sender(handler, capacity=3.0, window=3.0, spacing=0.0)
    start = time.monotonic()
    for i in range(4):
        await sender.send("Cg0KC2Zvbw", f"msg {i}")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.9  # the 4th send waited for a token
