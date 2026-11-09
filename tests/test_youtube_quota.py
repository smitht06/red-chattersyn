"""Per-pool quota tracker on Pacific time (YQB-31..34, 50, 51)."""

import datetime as dt
import logging
import random

import httpx
from PySide6.QtCore import QTimeZone

from chattersyn.services.youtube import quota as quota_mod
from chattersyn.services.youtube.api import QUOTA, QuotaTracker, YouTubeApi
from chattersyn.services.youtube.quota import Pool, next_pacific_midnight

UTC = dt.UTC
T0 = dt.datetime(2026, 11, 10, 7, 0, tzinfo=UTC)  # 23:00 PST on Nov 9


class Clock:
    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        return self.t


def _tracker(clock=None, seed=1):
    clock = clock or Clock()
    return QuotaTracker(now=clock, rng=random.Random(seed)), clock


async def _token():
    return "tok"


def _api(handler):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://www.googleapis.com/youtube/v3")
    return YouTubeApi(lambda: _token(), QuotaTracker(), client=client)


async def test_ban_and_timeout_cost_200():
    def handler(request):
        if request.method == "DELETE":
            return httpx.Response(204, request=request)
        return httpx.Response(200, json={"id": "b"}, request=request)

    assert QUOTA["ban"] == 200
    api = _api(handler)
    await api.live_chat_ban("lc", "UC1")
    assert api.quota.used == 200
    await api.live_chat_ban("lc", "UC1", duration=300)
    assert api.quota.used == 400
    await api.live_chat_unban("ban-1")
    assert api.quota.used == 600


def test_next_pacific_midnight_value():
    assert next_pacific_midnight(T0) == dt.datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    # Summer (PDT, UTC-7): midnight is 07:00Z
    summer = dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert next_pacific_midnight(summer) == dt.datetime(2026, 7, 2, 7, 0, tzinfo=UTC)


def test_missing_tz_database_falls_back_to_utc_minus_8(monkeypatch, caplog):
    # A frozen/Flatpak build with a stripped tz database: no America/Los_Angeles.
    monkeypatch.setattr(quota_mod, "_lookup_pacific", lambda: QTimeZone(b"No/Such_Zone"))
    monkeypatch.setattr(quota_mod, "_warned_no_tz", False)
    summer = dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    with caplog.at_level(logging.WARNING, logger=quota_mod.__name__):
        # Fixed UTC-8 (no daylight saving): midnight is 08:00Z even in summer.
        assert next_pacific_midnight(summer) == dt.datetime(2026, 7, 2, 8, 0, tzinfo=UTC)
        assert next_pacific_midnight(T0) == dt.datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # logged once, not per call


def test_local_midnight_is_not_reset():
    tracker, clock = _tracker()
    tracker.charge(5)
    clock.t = dt.datetime(2026, 11, 10, 7, 59, tzinfo=UTC)
    assert tracker.used == 5


def test_pacific_midnight_resets():
    tracker, clock = _tracker()
    tracker.charge(5)
    clock.t = dt.datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    assert tracker.used == 0


def test_resume_between_0001_and_0010_pt():
    for seed in range(20):
        tracker, _ = _tracker(seed=seed)
        resume = tracker.mark_exhausted(Pool.SHARED)
        assert dt.datetime(2026, 11, 10, 8, 1, tzinfo=UTC) <= resume
        assert resume <= dt.datetime(2026, 11, 10, 8, 10, tzinfo=UTC)
        assert tracker.resume_at(Pool.SHARED) == resume


def test_one_resume_time_per_pool():
    tracker, _ = _tracker()
    first = tracker.mark_exhausted(Pool.SHARED)
    assert tracker.mark_exhausted(Pool.SHARED) == first
    key = tracker.mark_exhausted(Pool.KEY)
    assert tracker.resume_at(Pool.KEY) == key
    assert tracker.resume_at(Pool.SHARED) == first


def test_key_reads_count_in_key_pool():
    tracker, _ = _tracker()
    tracker.charge(5, Pool.KEY)
    assert tracker.used_in(Pool.KEY) == 5
    assert tracker.used == 0
    assert tracker.used_in(Pool.SHARED) == 0


def test_exhausted_clears_at_resume_time():
    tracker, clock = _tracker()
    assert tracker.resume_at(Pool.SHARED) is None
    resume = tracker.mark_exhausted(Pool.SHARED)
    assert tracker.exhausted()
    assert not tracker.exhausted(Pool.KEY)
    clock.t = resume - dt.timedelta(seconds=1)
    assert tracker.exhausted()
    clock.t = resume
    assert not tracker.exhausted()
    assert tracker.resume_at(Pool.SHARED) is None


def test_on_exhausted_fires_once_per_reset():
    tracker, _ = _tracker()
    seen = []
    tracker.on_exhausted = lambda pool, at: seen.append((pool, at))
    resume = tracker.mark_exhausted(Pool.KEY)
    tracker.mark_exhausted(Pool.KEY)
    assert seen == [(Pool.KEY, resume)]
