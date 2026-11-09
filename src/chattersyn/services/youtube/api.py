"""YouTube Data API v3 REST client (httpx) + quota accounting.

Endpoints used (verified against the discovery document, 2026-10-15):
  videos.list              1 unit      liveStreamingDetails.activeLiveChatId
  channels.list            1 unit      uploads / mine / chat display names
  playlistItems.list       1 unit      uploads playlist scan (newest video)
  liveBroadcasts.list      1 unit      mine=true — the user's own broadcast
  search.list              1 unit      its own bucket of 100 calls/day
  liveChatMessages.list    5 units     REST fallback reader
  liveChatMessages.insert  20 units    send
  liveChatBans.insert      200 units   ban / timeout (user card)
  liveChatBans.delete      200 units   unban (user card)

All live chat methods are OAuth-only (API keys are rejected). The default
project quota is 10,000 units/day; the tracker below is approximate (a single
10k pool is shared by all APIs/keys of the project, and we cannot query it).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx

from .quota import Pool, QuotaTracker

log = logging.getLogger(__name__)

BASE_URL = "https://www.googleapis.com/youtube/v3"
TIMEOUT = httpx.Timeout(connect=10, read=20, write=20, pool=10)

QUOTA = {
    "list": 5,
    "insert": 20,
    "meta": 1,  # videos/channels/playlistItems/search
    "ban": 200,  # liveChatBans insert/delete
}

# Reasons that mean the user's API key itself is unusable (bad, expired, restricted,
# or the API not enabled for its project). Matched against error.errors[].reason
# (classic Google API errors) and error.details[].reason (google.rpc.ErrorInfo).
# Source: Google's ErrorReason reference
# (docs.cloud.google.com/php/docs/reference/common-protos/latest/Api.ErrorReason)
# lists API_KEY_INVALID, SERVICE_DISABLED, API_KEY_SERVICE_BLOCKED,
# API_KEY_HTTP_REFERRER_BLOCKED and API_KEY_IP_ADDRESS_BLOCKED. A live YouTube
# bad-key reply is HTTP 400 with error.errors[].reason "badRequest" and an
# ErrorInfo detail (domain googleapis.com) with reason "API_KEY_INVALID" - which
# is why error.details[].reason must be read. The legacy keyInvalid, keyExpired,
# accessNotConfigured and ipRefererBlocked are kept for older reply shapes.
KEY_INVALID_REASONS = {
    "keyInvalid", "keyExpired", "accessNotConfigured", "ipRefererBlocked",
    "API_KEY_INVALID", "API_KEY_EXPIRED", "SERVICE_DISABLED", "API_KEY_SERVICE_BLOCKED",
    "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED",
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

    @property
    def is_unauthenticated(self) -> bool:
        # "authError" = an API key used where OAuth is required (e.g. mine=true).
        return self.http_status == 401 or self.reason in ("unauthenticated", "authError")


class YouTubeApi:
    def __init__(
        self,
        get_token: Callable[[], Awaitable[str | None]],
        quota: QuotaTracker | None = None,
        *,
        get_key: Callable[[], str | None] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._get_token = get_token
        self._get_key = get_key
        self.quota = quota or QuotaTracker()
        self.key_problem = ""  # reason the API key was rejected; "" when fine
        self._owned = client is None
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT)

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    def reset_key_problem(self) -> None:
        self.key_problem = ""

    # -- plumbing ----------------------------------------------------------------

    def _usable_key(self) -> str | None:
        if self.key_problem or self._get_key is None:
            return None
        return self._get_key() or None

    async def read_pool(self, *, signin: bool = False) -> Pool | None:
        """The pool a public read would be charged to right now, or None when
        there is no credential at all."""
        if not signin and self._usable_key():
            return Pool.KEY
        if await self._get_token() is not None:
            return Pool.SHARED
        return None

    async def _auth(
        self, public: bool, signin: bool
    ) -> tuple[dict[str, str], dict[str, str], Pool]:
        """(headers, query params, pool). Public reads use the user's API key
        when it works (its own quota pool); everything else - and a rejected
        key - uses the OAuth Bearer (the shared pool). Live chat endpoints
        reject API keys server-side, so they never take the public path."""
        if public and not signin:
            key = self._usable_key()
            if key:
                return {}, {"key": key}, Pool.KEY
        token = await self._get_token()
        if token is not None:
            return {"Authorization": f"Bearer {token}"}, {}, Pool.SHARED
        if public and self.key_problem:
            raise YoutubeApiError(401, "unauthenticated", "API key not working")
        raise YoutubeApiError(401, "unauthenticated", "no YouTube token or API key available")

    def _check_pool(self, pool: Pool) -> None:
        if self.quota.exhausted(pool):
            raise YoutubeApiError(403, "quotaExceeded", "daily YouTube quota used up")

    async def _send(
        self, method: str, path: str, params: dict, units: int, public: bool,
        signin: bool, body: dict | None = None,
    ) -> tuple[httpx.Response, Pool]:
        headers, auth, pool = await self._auth(public, signin)
        self._check_pool(pool)
        self.quota.charge(units, pool)
        kwargs: dict = {"params": {**params, **auth}, "headers": headers}
        if body is not None:
            kwargs["json"] = body
        response = await self._client.request(method, path, **kwargs)
        return response, pool

    async def _request(
        self, method: str, path: str, params: dict, units: int, *,
        public: bool = False, signin: bool = False, body: dict | None = None,
    ) -> dict:
        response, pool = await self._send(method, path, params, units, public, signin, body)
        if method == "DELETE" and response.status_code < 300:
            return {}  # 204 No Content - nothing to parse
        try:
            return self._parse(response, path, pool)
        except YoutubeApiError:
            # A rejected key: repeat this read once with the sign-in, if any.
            if pool is Pool.KEY and self.key_problem and await self._get_token() is not None:
                response, pool = await self._send(
                    method, path, params, units, public, True, body)
                return self._parse(response, path, pool)
            if pool is Pool.KEY and self.key_problem:
                raise YoutubeApiError(401, "unauthenticated", "API key not working") from None
            raise

    async def _get(
        self, path: str, params: dict, units: int, *, public: bool = False, signin: bool = False
    ) -> dict:
        return await self._request("GET", path, params, units, public=public, signin=signin)

    async def _post(self, path: str, params: dict, body: dict, units: int) -> dict:
        return await self._request("POST", path, params, units, body=body)

    async def _delete(self, path: str, params: dict, units: int) -> dict:
        return await self._request("DELETE", path, params, units)

    def _parse(self, response: httpx.Response, path: str, pool: Pool = Pool.SHARED) -> dict:
        if response.status_code < 300:
            return response.json()
        reason, message = "unknown", ""
        reasons: set[str] = set()
        try:
            error = response.json()["error"]
            message = error.get("message", "")
            first = (error.get("errors") or [{}])[0]
            reason = first.get("reason", "unknown")
            for item in (error.get("errors") or []) + (error.get("details") or []):
                if isinstance(item, dict) and item.get("reason"):
                    reasons.add(item["reason"])
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        exc = YoutubeApiError(
            response.status_code,
            reason,
            message,
            retryable=reason == "rateLimitExceeded" or response.status_code in (429, 500, 502, 503),
        )
        if exc.is_quota:
            self.quota.mark_exhausted(pool)
        elif pool is Pool.KEY and reasons & KEY_INVALID_REASONS:
            self.key_problem = sorted(reasons & KEY_INVALID_REASONS)[0]
        log.debug("YouTube API %s -> %s %s", path, response.status_code, reason)
        raise exc

    # -- metadata / resolution --------------------------------------------------------

    async def videos(self, video_id: str, *, signin: bool = False) -> dict | None:
        """videos.list for one video; None when the video does not exist.
        signin=True forces the Google sign-in (the Stream tab's own reads)."""
        try:
            return await self._get(
                "/videos",
                {"part": "liveStreamingDetails,snippet", "id": video_id},
                QUOTA["meta"],
                public=True, signin=signin,
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
            QUOTA["meta"], public=True,
        )).get("items", [])

    async def channel_by_handle(self, handle: str) -> dict | None:
        try:
            return await self._get(
                "/channels",
                {"part": "contentDetails,snippet,statistics", "forHandle": handle.lstrip("@")},
                QUOTA["meta"], public=True,
            )
        except YoutubeApiError as exc:
            if exc.http_status in (400, 404):
                return None
            raise

    async def channels_names(self, channel_ids: list[str]) -> dict[str, str]:
        """channels.list part=snippet for display names (gRPC chat items carry
        only the channel id). Up to 50 ids per call (caller chunks; 51+
        returns 400). Returns {channel_id: title}; missing ids are omitted."""
        if not channel_ids:
            return {}
        data = await self._get(
            "/channels",
            {"part": "snippet", "id": ",".join(channel_ids)},
            QUOTA["meta"], public=True,
        )
        return {
            item["id"]: (item.get("snippet") or {}).get("title", "")
            for item in data.get("items", [])
            if item.get("id")
        }

    async def channel_by_id(self, channel_id: str) -> dict | None:
        try:
            return await self._get(
                "/channels",
                {"part": "contentDetails,snippet,statistics", "id": channel_id},
                QUOTA["meta"], public=True,
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
        return await self._get("/playlistItems", params, QUOTA["meta"], public=True)

    async def search_live(self, channel_id: str) -> list[dict]:
        """search.list eventType=live (own 100-calls/day bucket)."""
        return (await self._get(
            "/search",
            {"part": "snippet", "eventType": "live", "type": "video",
             "channelId": channel_id, "maxResults": "25"},
            QUOTA["meta"], public=True,
        )).get("items", [])

    async def channels_mine(self) -> dict:
        """channels.list mine=true: the authorized account's own channel."""
        return await self._get("/channels", {"part": "snippet", "mine": "true"}, QUOTA["meta"])

    async def live_broadcasts_mine(self) -> list[dict]:
        """liveBroadcasts.list mine=true: the authorized account's own
        broadcasts. OAuth-only (mine=true rejects API keys). 1 unit/call.

        No `broadcastStatus` filter: the live API rejects the mine +
        broadcastStatus combination (400 incompatibleParameters) even though
        the discovery document allows it — the resolver filters by
        lifeCycleStatus client-side instead."""
        return (await self._get(
            "/liveBroadcasts",
            {"part": "snippet,contentDetails,status,statistics", "mine": "true"},
            QUOTA["meta"],
        )).get("items", [])

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
