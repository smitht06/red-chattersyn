"""Check schedule: when a YouTube watch checks next (pure function)."""

import datetime as dt

from chattersyn.services.youtube.resolver import ResolveState
from chattersyn.services.youtube.schedule import WatchKind, next_check_at

T = dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.UTC)
SEC = dt.timedelta(seconds=1)
MIN = dt.timedelta(minutes=1)


def _next(kind=WatchKind.CHANNEL, state=ResolveState.OFFLINE, *, t=T,
          offline_since=None, starts=(), on_screen=True):
    return next_check_at(
        kind, state, last_check=t,
        offline_since=offline_since if offline_since is not None else t,
        scheduled_starts=list(starts), on_screen=on_screen,
    )


def test_yqb6_recently_offline():
    assert _next(offline_since=T - 10 * MIN) == T + 2 * MIN


def test_yqb7_offline_long():
    assert _next(offline_since=T - 31 * MIN) == T + 5 * MIN


def test_yqb8_upcoming_without_start_is_offline():
    assert _next(state=ResolveState.UPCOMING, offline_since=T - 10 * MIN) == T + 2 * MIN
    assert _next(state=ResolveState.UPCOMING, offline_since=T - 40 * MIN) == T + 5 * MIN


def test_yqb9_stream_tab_offline_long():
    got = _next(WatchKind.MINE, offline_since=T - 2 * 60 * MIN)
    assert got == T + 120 * SEC


def test_yqb10_scheduled_days_ahead():
    assert _next(starts=[T + 2 * 24 * 60 * MIN]) == T + 15 * MIN


def test_yqb11_close_to_start():
    assert _next(starts=[T + 4 * MIN]) == T + 30 * SEC


def test_yqb12_start_late():
    assert _next(starts=[T - 10 * MIN]) == T + 30 * SEC


def test_yqb13_never_starts():
    s = T - 16 * MIN
    assert _next(starts=[s]) == T + 2 * MIN
    t = s + 15 * MIN + 31 * MIN
    assert _next(t=t, offline_since=t - 60 * MIN, starts=[s]) == t + 5 * MIN
    # grace end resets "since": 31 min after it -> 5 min, 10 min after -> 2 min
    t2 = s + 15 * MIN + 10 * MIN
    assert _next(t=t2, offline_since=t2 - 60 * MIN, starts=[s]) == t2 + 2 * MIN


def test_yqb14_15_not_on_screen():
    assert _next(on_screen=False) == T + 15 * MIN
    assert _next(WatchKind.VIDEO, ResolveState.ENDED, on_screen=False) == T + 15 * MIN


def test_yqb16_hidden_near_start():
    assert _next(starts=[T + 4 * MIN], on_screen=False) == T + 15 * MIN


def test_yqb19_stream_tab_hidden():
    assert _next(WatchKind.MINE, on_screen=False) == T + 2 * MIN


def test_yqb36_several_starts():
    got = _next(starts=[T + 2 * 24 * 60 * MIN, T + 180 * MIN])
    assert got == T + 15 * MIN
    # near the 3 h start but far from the 2-day one: the 3 h start rules
    t = T + 175 * MIN
    got = _next(t=t, offline_since=T, starts=[T + 2 * 24 * 60 * MIN, T + 180 * MIN])
    assert got == T + 175 * MIN + 30 * SEC


def test_yqb37_own_scheduled_two_hours():
    assert _next(WatchKind.MINE, starts=[T + 120 * MIN]) == T + 2 * MIN


def test_yqb38_own_close_to_start():
    assert _next(WatchKind.MINE, starts=[T + 4 * MIN]) == T + 30 * SEC


def test_mine_clips_to_five_minute_mark():
    s = T + 6 * MIN
    assert _next(WatchKind.MINE, starts=[s]) == s - 5 * MIN


def test_mine_grace_ended_back_to_two_minutes():
    assert _next(WatchKind.MINE, starts=[T - 16 * MIN]) == T + 2 * MIN


def test_yqb40_channel_not_found():
    assert _next(state=ResolveState.NOT_FOUND) == T + 15 * MIN
    assert _next(state=ResolveState.NOT_FOUND, on_screen=False) == T + 15 * MIN


def test_yqb52_stale_start_ignored():
    got = _next(starts=[T - 2 * 24 * 60 * MIN, T + 180 * MIN],
                offline_since=T - 2 * 24 * 60 * MIN)
    assert got == T + 15 * MIN
    t = T + 176 * MIN
    got = _next(t=t, offline_since=T - 2 * 24 * 60 * MIN,
                starts=[T - 2 * 24 * 60 * MIN, T + 180 * MIN])
    assert got == t + 30 * SEC


def test_yqb53_five_minute_mark_not_missed():
    s = T + 12 * MIN
    assert _next(starts=[s]) == s - 5 * MIN
