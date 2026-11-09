"""When a YouTube watch checks next — a pure function (no I/O, no clock reads)."""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Sequence

from chattersyn.services.youtube.resolver import ResolveState

_FAST = dt.timedelta(seconds=30)
_MINE = dt.timedelta(seconds=120)
_HIDDEN = dt.timedelta(minutes=15)
_LEAD = dt.timedelta(minutes=5)
_GRACE = dt.timedelta(minutes=15)
_RECENT = dt.timedelta(minutes=2)
_LONG = dt.timedelta(minutes=5)
_RECENT_WINDOW = dt.timedelta(minutes=30)


class WatchKind(enum.Enum):
    CHANNEL = "channel"
    VIDEO = "video"
    MINE = "mine"


def next_check_at(
    kind: WatchKind,
    state: ResolveState,
    *,
    last_check: dt.datetime,
    offline_since: dt.datetime,
    scheduled_starts: Sequence[dt.datetime],
    on_screen: bool,
) -> dt.datetime:
    """The time of the next check after the check made at ``last_check``."""
    t = last_check
    live_starts = [s for s in scheduled_starts if s + _GRACE > t]
    earliest = min(live_starts) if live_starts else None

    if kind is WatchKind.MINE:
        if earliest is not None:
            if earliest - _LEAD <= t:
                return t + _FAST
            mark = earliest - _LEAD
            if mark < t + _MINE:
                return mark
        return t + _MINE

    if kind is WatchKind.CHANNEL and state is ResolveState.NOT_FOUND:
        return t + _HIDDEN
    if not on_screen:
        return t + _HIDDEN

    if earliest is not None:
        mark = earliest - _LEAD
        if t < mark:
            return min(t + _HIDDEN, mark)
        return t + _FAST

    since = offline_since
    ended = [s + _GRACE for s in scheduled_starts if s + _GRACE <= t]
    if ended:
        since = max(since, max(ended))
    return t + (_RECENT if t - since < _RECENT_WINDOW else _LONG)
