"""YouTube Data API v3 REST client (httpx) + quota accounting.

Endpoints used (verified against the discovery document, 2026-10-15):
  videos.list              1 unit      liveStreamingDetails.activeLiveChatId
  channels.list            1 unit      contentDetails.relatedPlaylists.uploads
  playlistItems.list       1 unit      uploads playlist scan (newest video)
  search.list              1 unit      100 calls/day separate bucket
  liveChatMessages.list    5 units     REST fallback reader
  liveChatMessages.insert  20 units    send
  liveChatBans.insert      50 units    ban (moderation mode)
  liveChatBans.delete      50 units    unban (moderation mode)

All live chat methods are OAuth-only (API keys are rejected). The default
project quota is 10,000 units/day; the tracker below is approximate (a single
10k pool is shared by all APIs/keys of the project, and we cannot query it).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://www.googleapis.com/youtube/v3"
TIMEOUT = httpx.Timeout(connect=10, read=20, write=20, pool=10)

QUOTA = {
    "list": 5,
    "insert": 20,
    "meta": 1,  # videos/channels/playlistItems/search
    "ban": 50,  # liveChatBans insert/delete
}

# Error reasons that mean the chat itself is gone/blocked (not retryable by us).
CHAT_UNAVAILABLE = {"liveChatEnded", "liveChatDisabled", "liveChatNotFound", "forbidden"}


class YoutubeApiError(Exception):
    """Typed API error. `reason` is the machine string from error.errors[].reason."""

    def __init__(self, http_status: int, reason: str, message: str, retryable: bool = False):
        super().__init__(f"{reason}: {message}")
        self.http_status = http_status
        self.reason = reason
        self.message = message
        self.retryable = retryable

    @property
    def is_quota(self) -> bool:
        return self.reason == "quotaExceeded"

    @property
    def is_chat_unavailable(self) -> bool:
        return self.reason in CHAT_UNAVAILABLE

    @property
    def is_rate_limited(self) -> bool:
        return self.reason == "rateLimitExceeded"


class QuotaTracker:
    """Approximate per-project-day quota accounting (in-memory)."""

    DAILY_UNITS = 10_000

    def __init__(self) -> None:
        self._used = 0
        self._day: dt.date | None = None
        self._exhausted = False

    def _rollover(self) -> None:
        today = dt.datetime.now().date()
        if self._day != today:
            self._day = today
            self._used = 0
            self._exhausted = False

    def charge(self, units: int) -> None:
        self._rollover()
        self._used += units

    def exhausted(self) -> bool:
        self._rollover()
        return self._exhausted

    def mark_exhausted(self) -> None:
        self._exhausted = True

    @property
    def used(self) -> int:
        self._rollover()
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self.DAILY_UNITS - self.used)


class YouTubeApi:
    def __init__(
        self,
        get_token: Callable[[], Awaitable[str | None]],
        quota: QuotaTracker | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._get_token = get_token
        self.quota = quota or QuotaTracker()
        self._owned = client is None
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT)

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    # -- plumbing ----------------------------------------------------------------

    async def _headers(self) -> dict[str, str]:
        token = await self._get_token()
        if token is None:
            raise YoutubeApiError(401, "unauthenticated", "no YouTube token available")
        return {"Authorization": f"Bearer {token}"}

    async def _get(self, path: str, params: dict, units: int) -> dict:
        self.quota.charge(units)
        headers = await self._headers()
        response = await self._client.get(path, params=params, headers=headers)
        return self._parse(response, path)

    async def _post(self, path: str, params: dict, body: dict, units: int) -> dict:
        self.quota.charge(units)
        headers = await self._headers()
        response = await self._client.post(path, params=params, json=body, headers=headers)
        return self._parse(response, path)

    async def _delete(self, path: str, params: dict, units: int) -> dict:
        self.quota.charge(units)
        headers = await self._headers()
        response = await self._client.delete(path, params=params, headers=headers)
        if response.status_code < 300:
            return {}  # 204 No Content — nothing to parse
        return self._parse(response, path)

    def _parse(self, response: httpx.Response, path: str) -> dict:
        if response.status_code < 300:
            return response.json()
        reason, message = "unknown", ""
        try:
            error = response.json()["error"]
            message = error.get("message", "")
            first = (error.get("errors") or [{}])[0]
            reason = first.get("reason", "unknown")
        except (ValueError, KeyError, TypeError):
            pass
        exc = YoutubeApiError(
            response.status_code,
            reason,
            message,
            retryable=reason == "rateLimitExceeded" or response.status_code in (429, 500, 502, 503),
        )
        if exc.is_quota:
            self.quota.mark_exhausted()
        log.debug("YouTube API %s -> %s %s", path, response.status_code, reason)
        raise exc

    # -- metadata / resolution --------------------------------------------------------

    async def videos(self, video_id: str) -> dict | None:
        """videos.list for one video; None when the video does not exist."""
        try:
            return await self._get(
                "/videos",
                {"part": "liveStreamingDetails,snippet", "id": video_id},
                QUOTA["meta"],
            )
        except YoutubeApiError as exc:
            if exc.http_status == 404:
                return None
            raise

    async def videos_batch(self, ids: list[str]) -> list[dict]:
        """videos.list for up to 50 ids (caller chunks; 51+ returns 400)."""
        if not ids:
            return []
        return (await self._get(
            "/videos",
            {"part": "liveStreamingDetails,snippet", "id": ",".join(ids)},
            QUOTA["meta"],
        )).get("items", [])

    async def channel_by_handle(self, handle: str) -> dict | None:
        try:
            return await self._get(
                "/channels",
                {"part": "contentDetails,snippet,statistics", "forHandle": handle.lstrip("@")},
                QUOTA["meta"],
            )
        except YoutubeApiError as exc:
            if exc.http_status in (400, 404):
                return None
            raise

    async def channel_by_id(self, channel_id: str) -> dict | None:
        try:
            return await self._get(
                "/channels",
                {"part": "contentDetails,snippet,statistics", "id": channel_id},
                QUOTA["meta"],
            )
        except YoutubeApiError as exc:
            if exc.http_status in (400, 404):
                return None
            raise

    async def playlist_items(
        self, playlist_id: str, page_token: str | None = None, max_results: int = 50
    ) -> dict:
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": str(max_results),
        }
        if page_token:
            params["pageToken"] = page_token
        return await self._get("/playlistItems", params, QUOTA["meta"])

    async def search_live(self, channel_id: str) -> list[dict]:
        """search.list eventType=live (own 100-calls/day bucket)."""
        return (await self._get(
            "/search",
            {"part": "snippet", "eventType": "live", "type": "video",
             "channelId": channel_id, "maxResults": "25"},
            QUOTA["meta"],
        )).get("items", [])

    async def channels_mine(self) -> dict:
        """channels.list mine=true: the authorized account's own channel."""
        return await self._get("/channels", {"part": "snippet", "mine": "true"}, QUOTA["meta"])

    # -- live chat ----------------------------------------------------------------------

    async def live_chat_list(
        self, live_chat_id: str, page_token: str | None = None, max_results: int = 500
    ) -> dict:
        params = {"part": "snippet", "liveChatId": live_chat_id, "maxResults": str(max_results)}
        if page_token:
            params["pageToken"] = page_token
        return await self._get("/liveChat/messages", params, QUOTA["list"])

    async def live_chat_insert(self, live_chat_id: str, text: str) -> dict:
        """POST one text message. Returns the inserted resource (with id)."""
        return await self._post(
            "/liveChat/messages",
            {"part": "snippet"},
            {
                "snippet": {
                    "liveChatId": live_chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {"messageText": text},
                }
            },
            QUOTA["insert"],
        )

    # -- moderation ----------------------------------------------------------------------

    async def live_chat_ban(self, live_chat_id: str, channel_id: str, duration: int = 0) -> dict:
        """Ban a user from one live chat. duration <= 0 = permanent; the
        insert response's `id` is required for live_chat_unban."""
        snippet: dict = {
            "liveChatId": live_chat_id,
            "type": "permanent" if duration <= 0 else "temporary",
            "bannedUserDetails": {"channelId": channel_id},
        }
        if duration > 0:
            snippet["banDurationSeconds"] = duration
        return await self._post(
            "/liveChat/bans",
            {"part": "snippet"},
            {"snippet": snippet},
            QUOTA["ban"],
        )

    async def live_chat_unban(self, ban_id: str) -> dict:
        """DELETE a ban by the id returned from live_chat_ban."""
        return await self._delete("/liveChat/bans", {"id": ban_id}, QUOTA["ban"])
