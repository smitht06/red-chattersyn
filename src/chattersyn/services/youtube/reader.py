"""YouTube live chat readers: gRPC liveChatMessages.streamList (primary) and
REST liveChatMessages.list (fallback).

gRPC facts (verified against the official Streaming Live Chat guide):
- one server-streaming RPC per liveChatId; 5 quota units per (re)connection
- resume after a drop by passing the last next_page_token as page_token
- auth: x-goog-api-key (API key) or authorization Bearer (OAuth) metadata
- FAILED_PRECONDITION means chat ended OR disabled (indistinguishable);
  RESOURCE_EXHAUSTED fires when reconnecting faster than YouTube's refresh
  rates — hence the caller's backoff matters.

REST fallback facts: OAuth only, 5 units/poll, pace by pollingIntervalMillis
(floor 12 s), drain up to a few nextPageToken pages per cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

import grpc
from google.rpc import error_details_pb2, status_pb2

from . import stream_list_pb2 as pb
from . import stream_list_pb2_grpc
from .api import KEY_INVALID_REASONS, YouTubeApi
from .mapper import pb2_to_item

log = logging.getLogger(__name__)

MIN_POLL_INTERVAL = 12.0  # seconds
MAX_DRAIN_PAGES = 3  # REST nextPageToken pages consumed per poll cycle

OnItem = Callable[[dict], None]


class StreamEnded(Exception):
    """The chat itself ended or was disabled (not retryable at reader level)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ReadRefused(Exception):
    """The credential was refused for this read. key_invalid: the API key
    itself is bad (as opposed to the key merely not being allowed here)."""

    def __init__(self, reason: str, *, key_invalid: bool):
        super().__init__(reason)
        self.reason = reason
        self.key_invalid = key_invalid


class ReadQuotaExceeded(Exception):
    """The credential's daily quota is used up."""


class GrpcReader:
    """One session of V3DataLiveChatMessageService.StreamList."""

    def __init__(self, channel: grpc.aio.Channel, get_metadata):
        self._channel = channel
        self._get_metadata = get_metadata  # async () -> tuple[tuple[str, str], ...]

    async def read(
        self,
        live_chat_id: str,
        page_token: str | None,
        on_item: OnItem,
        stop_event: asyncio.Event | None = None,
    ) -> tuple[str | None, str | None]:
        """Stream until server EOF, error or stop. Returns (next_page_token,
        offline_at) so the caller can reconnect from where the stream ended."""
        stub = stream_list_pb2_grpc.V3DataLiveChatMessageServiceStub(self._channel)
        request = pb.LiveChatMessageListRequest(
            part=["snippet"],
            live_chat_id=live_chat_id,
            page_token=page_token,
        )
        metadata = await self._get_metadata()
        call = stub.StreamList(request, metadata=metadata)
        next_token = page_token
        offline_at: str | None = None
        try:
            async for response in call:
                if response.HasField("next_page_token"):
                    next_token = response.next_page_token
                if response.HasField("offline_at"):
                    offline_at = response.offline_at
                if stop_event is not None and stop_event.is_set():
                    return next_token, offline_at
                for item in response.items:
                    on_item(pb2_to_item(item))
            return next_token, offline_at
        except grpc.aio.AioRpcError as exc:
            raise _translate(exc) from exc
        finally:
            with contextlib.suppress(Exception):
                call.cancel()


class RestReader:
    """Polling liveChatMessages.list with page draining and interval pacing."""

    def __init__(self, api: YouTubeApi):
        self._api = api

    async def read(
        self,
        live_chat_id: str,
        page_token: str | None,
        on_item: OnItem,
        stop_event: asyncio.Event | None = None,
        min_interval: float = MIN_POLL_INTERVAL,
    ) -> str | None:
        """Poll until stop_event is set (long-lived fallback loop).

        Returns the last page token seen, so the caller can persist it and
        resume mid-chat instead of re-reading the backlog on the next cycle.
        """
        token = page_token
        while stop_event is None or not stop_event.is_set():
            page = await self._api.live_chat_list(live_chat_id, page_token=token)
            interval = max(min_interval, int(page.get("pollingIntervalMillis", 0)) / 1000)
            fetches = 0
            while True:  # emit this page, then drain up to MAX_DRAIN_PAGES more
                for item in page.get("items", []):
                    on_item(item)
                token = page.get("nextPageToken")
                if not token or fetches >= MAX_DRAIN_PAGES:
                    break
                page = await self._api.live_chat_list(live_chat_id, page_token=token)
                fetches += 1
            if stop_event is None:
                return token  # single cycle: tests and one-shot backfills
            # Wait out the poll interval (or wake early when told to stop).
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
        return token


def _error_info(exc: grpc.aio.AioRpcError) -> tuple[str, str]:
    """(reason, domain) of the google.rpc.ErrorInfo in the error trailer."""
    for key, value in exc.trailing_metadata() or ():
        if key != "grpc-status-details-bin":
            continue
        try:
            status = status_pb2.Status.FromString(value)
        except Exception:
            return "", ""
        for detail in status.details:
            info = error_details_pb2.ErrorInfo()
            if detail.Unpack(info):
                return info.reason, info.domain
    return "", ""


def error_reason(exc: grpc.aio.AioRpcError) -> str:
    """The ErrorInfo reason (e.g. API_KEY_INVALID); "" when absent."""
    return _error_info(exc)[0]


def _translate(exc: grpc.aio.AioRpcError) -> Exception:
    code = exc.code()
    details = exc.details() or ""
    reason, domain = _error_info(exc)
    if code is grpc.StatusCode.FAILED_PRECONDITION:
        return StreamEnded("chat ended or disabled (indistinguishable)")
    if code in (
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.INVALID_ARGUMENT,
    ):
        if reason in KEY_INVALID_REASONS:
            return ReadRefused(reason, key_invalid=True)
        if code is grpc.StatusCode.PERMISSION_DENIED:
            return ReadRefused(f"not authorized: {details}", key_invalid=False)
        # UNAUTHENTICATED / INVALID_ARGUMENT without a key reason: as before.
        if code is grpc.StatusCode.UNAUTHENTICATED:
            return StreamEnded(f"not authorized: {details}")
        return exc
    if code is grpc.StatusCode.NOT_FOUND:
        return StreamEnded(f"live chat not found: {details}")
    # RESOURCE_EXHAUSTED without a quota reason = reconnecting too fast:
    # transient, back off harder.
    if code is grpc.StatusCode.RESOURCE_EXHAUSTED and "quota" in f"{reason} {domain}".lower():
        return ReadQuotaExceeded(details)
    return exc  # transient (UNAVAILABLE, INTERNAL, ...) — caller backs off and retries
