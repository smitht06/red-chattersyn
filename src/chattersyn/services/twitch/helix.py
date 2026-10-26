"""Twitch Helix API client: user OAuth token, badges, channel emotes, users."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx

from ...models.message import Badge

log = logging.getLogger(__name__)

BASE = "https://api.twitch.tv/helix"

TokenProvider = Callable[[], Awaitable[str | None]]


class HelixError(Exception):
    pass


class Helix:
    """Minimal Helix client for the metadata the chat needs.

    Uses the signed-in user's OAuth token, pulled lazily through `get_token`
    (the service validates/refreshes transparently). A rejected token forces
    one re-pull and retry; no token means no request is attempted.
    """

    def __init__(self, client_id: str, get_token: TokenProvider,
                 client: httpx.AsyncClient | None = None):
        self.client_id = client_id
        self._get_token = get_token
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0))
        self._owns_client = client is None
        self._token: str | None = None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- auth ---------------------------------------------------------------------

    def invalidate_token(self) -> None:
        """Drop the cached token so the next call re-pulls the provider
        (used when the service swaps tokens on reconnect/disconnect)."""
        self._token = None

    async def _pull_token(self) -> str:
        token = await self._get_token()
        if not token:
            raise HelixError("no Twitch user token — connect an account")
        self._token = token
        return token

    async def _get(self, path: str, params: dict) -> dict:
        response = await self._call("GET", path, params)
        response.raise_for_status()
        return response.json()

    async def _write(self, method: str, path: str, params: dict, body: dict | None = None) -> None:
        """POST/DELETE; failures raise HelixError carrying Twitch's message
        (e.g. "The user specified in the user_id field is already banned.")."""
        response = await self._call(method, path, params, body)
        if response.is_error:
            try:
                message = response.json().get("message", "")
            except ValueError:
                message = ""
            raise HelixError(message or f"HTTP {response.status_code}")

    async def _call(self, method: str, path: str, params: dict,
                    body: dict | None = None) -> httpx.Response:
        token = self._token or await self._pull_token()
        response = await self._request(path, params, token, method, body)
        if response.status_code == 401:
            # Token rejected (e.g. revoked): force one re-pull (the provider
            # may refresh) and retry.
            self._token = None
            token = await self._pull_token()
            response = await self._request(path, params, token, method, body)
        return response

    async def _request(self, path: str, params: dict, token: str, method: str = "GET",
                       body: dict | None = None) -> httpx.Response:
        return await self._client.request(
            method,
            f"{BASE}{path}",
            params=params,
            json=body,
            headers={"Client-Id": self.client_id, "Authorization": f"Bearer {token}"},
        )

    # -- badges ---------------------------------------------------------------------

    @staticmethod
    def _badges_from(data: dict) -> dict[str, dict[str, Badge]]:
        """{set_id: {version_id: Badge}} from a badges response."""
        result: dict[str, dict[str, Badge]] = {}
        for item in data.get("data", []):
            set_id = item.get("set_id", "")
            versions: dict[str, Badge] = {}
            for version in item.get("versions", []):
                v_id = version.get("id", "")
                urls = {
                    scale: version[f"image_url_{scale}"]
                    for scale in ("1x", "2x", "4x")
                    if version.get(f"image_url_{scale}")
                }
                versions[v_id] = Badge(
                    set_id=set_id,
                    version_id=v_id,
                    title=version.get("title", set_id),
                    tooltip=version.get("description", "") or version.get("title", ""),
                    urls=urls,
                )
            result[set_id] = versions
        return result

    async def global_badges(self) -> dict[str, dict[str, Badge]]:
        return self._badges_from(await self._get("/chat/badges/global", {}))

    async def channel_badges(self, broadcaster_id: str) -> dict[str, dict[str, Badge]]:
        return self._badges_from(
            await self._get("/chat/badges", {"broadcaster_id": broadcaster_id})
        )

    # -- emotes ----------------------------------------------------------------------

    async def channel_emotes(self, broadcaster_id: str) -> dict[str, str]:
        """Channel emotes: {emote_id: name} for resolving IRC emote tags."""
        data = await self._get("/chat/emotes", {"broadcaster_id": broadcaster_id})
        return {e.get("id"): e.get("name", "") for e in data.get("data", []) if e.get("id")}

    async def global_emotes(self) -> dict[str, str]:
        """Global Twitch emotes: {emote_id: name} for the picker's Global tab."""
        data = await self._get("/chat/emotes/global", {})
        return {e.get("id"): e.get("name", "") for e in data.get("data", []) if e.get("id")}

    # -- users -----------------------------------------------------------------------

    async def user_by_login(self, login: str) -> dict | None:
        """{id, login, display_name, profile_image_url, created_at} or None."""
        data = await self._get("/users", {"login": login})
        users = data.get("data", [])
        if not users:
            return None
        user = users[0]
        return {
            "id": user.get("id", ""),
            "login": user.get("login", ""),
            "display_name": user.get("display_name", ""),
            "profile_image_url": user.get("profile_image_url", ""),
            "created_at": user.get("created_at", ""),
        }

    async def user_follows(self, from_id: str, to_id: str) -> str | None:
        """The date from_id followed to_id (ISO), or None if they don't."""
        data = await self._get("/users/follows", {"from_id": from_id, "to_id": to_id})
        rows = data.get("data", [])
        if not rows:
            return None
        return rows[0].get("followed_at") or None

    async def channel_followers(self, broadcaster_id: str) -> int | None:
        """Total follower count for a channel, or None when unavailable."""
        data = await self._get("/channels/followers", {"broadcaster_id": broadcaster_id})
        total = data.get("total")
        return int(total) if isinstance(total, (int, str)) and str(total).isdigit() else None

    # -- moderation ----------------------------------------------------------------------
    # Both need the moderator:manage:banned_users scope; moderator_id must be
    # the token's own user id.

    async def ban_user(self, broadcaster_id: str, moderator_id: str, user_id: str,
                       duration: int = 0, reason: str = "") -> None:
        """Ban (duration 0) or time out (1..1209600 s) a user."""
        data: dict = {"user_id": user_id}
        if duration:
            data["duration"] = duration
        if reason:
            data["reason"] = reason
        await self._write(
            "POST", "/moderation/bans",
            {"broadcaster_id": broadcaster_id, "moderator_id": moderator_id},
            {"data": data})

    async def unban_user(self, broadcaster_id: str, moderator_id: str, user_id: str) -> None:
        """Lift a ban or an active timeout."""
        await self._write(
            "DELETE", "/moderation/bans",
            {"broadcaster_id": broadcaster_id, "moderator_id": moderator_id,
             "user_id": user_id})

    # -- streams -----------------------------------------------------------------------

    async def streams(self, user_login: str) -> dict | None:
        """Live stream for a login: {title, viewer_count, started_at} or
        None when the channel is offline (user token)."""
        data = await self._get("/streams", {"user_login": user_login})
        rows = data.get("data", [])
        if not rows:
            return None
        row = rows[0]
        return {
            "title": row.get("title") or "",
            "viewer_count": int(row.get("viewer_count") or 0),
            "started_at": row.get("started_at") or "",
        }
