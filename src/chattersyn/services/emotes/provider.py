"""Shared bits for emote providers."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from ...models.emote import Emote

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)


class EmoteProvider(ABC):
    """Fetches the emote set for one channel from a third-party service."""

    name: str = ""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT)
        return self._client

    @abstractmethod
    async def fetch_for_channel(self, channel_name: str, twitch_id: str) -> list[Emote]:
        """Channel-scoped emotes for one channel; empty list on missing/error
        (providers are best-effort, never fatal)."""

    async def fetch_global(self) -> list[Emote]:
        """Global (scope=None) emotes. Default: none — providers that expose a
        global set (BTTV/FFZ/7TV) override this; fetched once, reused across
        channels."""
        return []
