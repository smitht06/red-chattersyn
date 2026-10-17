"""Rate limiting primitives for chat sending (Twitch 20/30s, YouTube ~11/30s)."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Classic token bucket; `acquire` awaits until tokens are available."""

    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self._last = time.monotonic()
        self._waiters: list[asyncio.Future] = []

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.refill_per_sec)
        self._last = now

    def try_acquire(self, n: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def available(self) -> float:
        self._refill()
        return self.tokens

    def seconds_until(self, n: float = 1.0) -> float:
        self._refill()
        if self.tokens >= n:
            return 0.0
        return (n - self.tokens) / self.refill_per_sec

    async def acquire(self, n: float = 1.0) -> None:
        while True:
            if self.try_acquire(n):
                return
            await asyncio.sleep(min(0.25, self.seconds_until(n)))


class MinInterval:
    """Enforces a minimum spacing between operations (e.g. 1.1 s per send)."""

    def __init__(self, interval: float):
        self.interval = float(interval)
        self._last = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        remaining = self._last + self.interval - now
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last = time.monotonic()
