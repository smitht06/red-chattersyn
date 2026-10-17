"""Twitch Helix API client: app access token, badges, channel emotes, users."""

from __future__ import annotations

import logging
import time

import httpx

from ...models.message import Badge

log = logging.getLogger(__name__)

BASE = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


class HelixError(Exception):
    pass


class Helix:
    """Minimal Helix client for the metadata the chat needs.

    Uses an app access token (client credentials) — no user OAuth required for
    badges/emotes/user lookups.
    """

    def __init__(self, client_id: str, client_secret: str, client: httpx.AsyncClient | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0))
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expiry: float = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- auth ---------------------------------------------------------------------

    async def _ensure_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry - 60:
            return self._token
        response = await self._client.post(
            TOKEN_URL,
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
        )
        response.raise_for_status()
        data = response.json()
        self._token = data.get("access_token", "")
        self._token_expiry = time.monotonic() + int(data.get("expires_in", 0))
        if not self._token:
            raise HelixError("no access token in response")
        return self._token

    async def _get(self, path: str, params: dict) -> dict:
        token = await self._ensure_token()
        response = await self._client.get(
            f"{BASE}{path}",
            params=params,
            headers={"Client-Id": self.client_id, "Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            # Token rejected (e.g. app credentials rotated): force a refresh once.
            self._token = None
            token = await self._ensure_token()
            response = await self._client.get(
                f"{BASE}{path}",
                params=params,
                headers={"Client-Id": self.client_id, "Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        return response.json()

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

    # -- streams -----------------------------------------------------------------------

    async def streams(self, user_login: str) -> dict | None:
        """Live stream for a login: {title, viewer_count, started_at} or
        None when the channel is offline (app token — no user OAuth)."""
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
