"""YouTube display-name resolution for chat authors.

gRPC streamList carries only snippet.authorChannelId, so the mapper
synthesizes an author named after the channel id. This component resolves
those ids to display names via channels.list (1 quota unit per 50 ids).

Messages from uncached ids are held for FLUSH_DELAY so one API call covers
a burst of first-time chatters — the first message from a new chatter
appears ~300 ms late, every later one is a cache hit. A failed fetch or
exhausted quota falls back to emitting with the channel id as the name, so
chat never stalls on cosmetics. Held messages can land behind messages
that arrived after them (the sub-second batch window); ordering within a
batch is preserved.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ...core.bus import bridge
from ...models.message import Message
from .api import YouTubeApi

log = logging.getLogger(__name__)

FLUSH_DELAY = 0.3  # seconds to batch pending ids into one channels.list call
BATCH = 50  # channels.list id limit
FETCH_TIMEOUT = 5.0


class YouTubeNames:
    def __init__(self, api: YouTubeApi) -> None:
        self._api = api
        self._cache: dict[str, str] = {}
        self._pending: dict[str, list[Message]] = {}
        self._task: asyncio.Task | None = None

    def cached(self, channel_id: str) -> str | None:
        return self._cache.get(channel_id)

    def hold(self, msg: Message) -> None:
        """Defer emitting `msg` until its author's name is resolved."""
        cid = msg.author.user_id
        self._pending.setdefault(cid, []).append(msg)
        if self._task is None:
            self._task = asyncio.ensure_future(self._flush())

    async def _flush(self) -> None:
        try:
            await asyncio.sleep(FLUSH_DELAY)
            batch, self._pending = self._pending, {}
            if batch:
                await self._resolve(batch)
        finally:
            self._task = None
            if self._pending:  # holds arrived mid-flush: run again for them
                self._task = asyncio.ensure_future(self._flush())

    async def _resolve(self, batch: dict[str, list[Message]]) -> None:
        names: dict[str, str] = {}
        if not self._api.quota.exhausted():
            uncached = [cid for cid in batch if cid not in self._cache]
            for start in range(0, len(uncached), BATCH):
                chunk = uncached[start:start + BATCH]
                try:
                    names.update(
                        await asyncio.wait_for(
                            self._api.channels_names(chunk), timeout=FETCH_TIMEOUT))
                except Exception as exc:
                    log.warning("YouTube display-name fetch failed: %s", exc)
        self._cache.update(names)
        for cid, messages in batch.items():
            name = self._cache.get(cid)
            for msg in messages:
                if name:
                    msg.author.name = name
                bridge.message.emit(msg)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
