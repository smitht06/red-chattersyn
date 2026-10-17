"""YouTube live chat sender: liveChatMessages.insert (20 units, OAuth).

The 200-character cap and the URL/HTML ban are documented on YouTube's
consumer help page but NOT in the API reference — the server-side
messageTextInvalid error is authoritative, so we pre-block the common cases
(URLs, >200) with clear messages and surface the API's exact reason when it
still rejects. Rate limiting: conservative bucket (11 messages/30 s) plus
2 s spacing — the server's rateLimitExceeded is the authoritative backstop.
"""

from __future__ import annotations

import logging

from ...core.ratelimit import MinInterval, TokenBucket
from ...core.text import YOUTUBE_MESSAGE_LIMIT, contains_url
from .api import YouTubeApi, YoutubeApiError

log = logging.getLogger(__name__)


class YoutubeSendError(Exception):
    """Typed send failure. `retryable` = the coordinator may retry with backoff."""

    def __init__(self, reason: str, detail: str, retryable: bool = False):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.retryable = retryable


class YoutubeSender:
    def __init__(self, api: YouTubeApi, *, block_urls: bool = True,
                 capacity: float = 11.0, window: float = 30.0,
                 spacing: float = 2.0) -> None:
        self._api = api
        self._block_urls = block_urls
        self._bucket = TokenBucket(capacity=capacity, refill_per_sec=capacity / window)
        self._spacing = MinInterval(spacing)

    def validate(self, text: str) -> YoutubeSendError | None:
        """Client-side pre-checks; None = pass. (Server stays authoritative.)"""
        if len(text) > YOUTUBE_MESSAGE_LIMIT:
            return YoutubeSendError(
                "too_long",
                f"too long for YouTube ({len(text)}/{YOUTUBE_MESSAGE_LIMIT} characters)")
        if self._block_urls and contains_url(text):
            return YoutubeSendError(
                "url_blocked",
                "contains a URL — YouTube rejects links in live chat")
        return None

    async def send(self, live_chat_id: str, text: str) -> dict:
        """POST one message. Returns the inserted resource dict (has id).
        Raises YoutubeSendError for every failure mode."""
        error = self.validate(text)
        if error is not None:
            raise error
        await self._bucket.acquire()
        await self._spacing.wait()
        try:
            return await self._api.live_chat_insert(live_chat_id, text)
        except YoutubeApiError as exc:
            raise _map_error(exc) from exc


def _map_error(exc: YoutubeApiError) -> YoutubeSendError:
    if exc.is_quota:
        return YoutubeSendError("quota_exhausted",
                                "YouTube quota exhausted (resets midnight PT)")
    if exc.is_rate_limited:
        return YoutubeSendError("rate_limited",
                                "YouTube rate limit hit — will retry", retryable=True)
    if exc.reason == "messageTextInvalid":
        return YoutubeSendError(
            "message_invalid",
            f"YouTube rejected the message: {exc.message or 'invalid text'}"
            " — remove links/formatting")
    if exc.reason == "liveChatEnded":
        return YoutubeSendError("chat_ended", "the live chat has ended")
    if exc.reason == "liveChatDisabled":
        return YoutubeSendError("chat_disabled", "live chat is disabled on this stream")
    if exc.reason == "liveChatNotFound":
        return YoutubeSendError("chat_not_found", "live chat not found — stream may have ended")
    if exc.reason == "forbidden":
        return YoutubeSendError("forbidden", "not authorized to post in this chat")
    if exc.http_status == 403 and "reason" in exc.message:
        return YoutubeSendError("rejected", f"YouTube rejected the message: {exc.message}")
    if exc.retryable:
        # A 5xx may have committed the non-idempotent POST — retrying could
        # double-post. rateLimitExceeded above is the only safe retry.
        return YoutubeSendError("transient", f"YouTube error: {exc.message}")
    return YoutubeSendError("failed", f"YouTube send failed: {exc.message}")
