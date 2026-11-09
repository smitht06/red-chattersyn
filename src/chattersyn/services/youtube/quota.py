"""Per-pool YouTube quota bookkeeping on Pacific time.

Google resets the daily quota at midnight Pacific time, so counts restart there
(not at local midnight). Two pools are tracked: SHARED (the app's own Google
project, every sign-in) and KEY (the user's own API key's project).
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
import random
from collections.abc import Callable

from PySide6.QtCore import QDateTime, QTime, QTimeZone

log = logging.getLogger(__name__)

UTC = dt.UTC
RESUME_JITTER = (60.0, 600.0)  # seconds after the reset, to spread clients out
PACIFIC_FALLBACK_OFFSET = -8 * 3600  # PST, used only without a tz database
_warned_no_tz = False


class Pool(enum.Enum):
    SHARED = "shared"
    KEY = "key"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def _lookup_pacific() -> QTimeZone:
    return QTimeZone(b"America/Los_Angeles")


def _pacific_zone() -> QTimeZone:
    """America/Los_Angeles, or a fixed UTC-8 when the tz database lacks it
    (a frozen/Flatpak build with a stripped one); warns once."""
    global _warned_no_tz
    tz = _lookup_pacific()
    if tz.isValid():
        return tz
    if not _warned_no_tz:
        _warned_no_tz = True
        log.warning("time zone America/Los_Angeles not available; "
                    "YouTube quota resets use a fixed UTC-8")
    return QTimeZone(PACIFIC_FALLBACK_OFFSET)


def next_pacific_midnight(now: dt.datetime) -> dt.datetime:
    """The next 00:00 America/Los_Angeles after `now` (aware UTC in and out)."""
    tz = _pacific_zone()
    pt = QDateTime.fromSecsSinceEpoch(int(now.timestamp()), QTimeZone.utc()).toTimeZone(tz)
    nxt = QDateTime(pt.date().addDays(1), QTime(0, 0), tz).toUTC().toPython()
    return nxt.replace(tzinfo=UTC)


class _PoolState:
    def __init__(self) -> None:
        self.used = 0
        self.window_end: dt.datetime | None = None
        self.resume: dt.datetime | None = None


class QuotaTracker:
    """Approximate quota accounting per pool (in-memory)."""

    DAILY_UNITS = 10_000

    def __init__(
        self,
        *,
        now: Callable[[], dt.datetime] = _utc_now,
        rng: random.Random | None = None,
    ) -> None:
        self._now = now
        self._rng = rng or random.Random()
        self._pools = {pool: _PoolState() for pool in Pool}
        self.on_exhausted: Callable[[Pool, dt.datetime], None] | None = None

    def _state(self, pool: Pool) -> _PoolState:
        st = self._pools[pool]
        now = self._now()
        if st.window_end is not None and now >= st.window_end:
            st.used = 0
            st.window_end = None
        if st.resume is not None and now >= st.resume:
            st.resume = None
        return st

    def charge(self, units: int, pool: Pool = Pool.SHARED) -> None:
        st = self._state(pool)
        if st.window_end is None:
            st.window_end = next_pacific_midnight(self._now())
        st.used += units

    def used_in(self, pool: Pool) -> int:
        return self._state(pool).used

    @property
    def used(self) -> int:
        return self.used_in(Pool.SHARED)

    @property
    def remaining(self) -> int:
        return max(0, self.DAILY_UNITS - self.used)

    def exhausted(self, pool: Pool = Pool.SHARED) -> bool:
        return self._state(pool).resume is not None

    def resume_at(self, pool: Pool) -> dt.datetime | None:
        return self._state(pool).resume

    def mark_exhausted(self, pool: Pool) -> dt.datetime:
        """Mark the pool used up; returns when to resume (same value until then)."""
        st = self._state(pool)
        if st.resume is not None:
            return st.resume
        jitter = self._rng.uniform(*RESUME_JITTER)
        st.resume = next_pacific_midnight(self._now()) + dt.timedelta(seconds=jitter)
        if self.on_exhausted is not None:
            self.on_exhausted(pool, st.resume)
        return st.resume
