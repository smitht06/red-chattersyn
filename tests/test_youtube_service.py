"""YouTubeService watch loop: resolution states, reconnect token, chat end,
REST fallback after gRPC failures, quota waits per pool, cancellation."""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest

from chattersyn.config.settings import Settings
from chattersyn.core.bus import bridge
from chattersyn.services.youtube import names as names_mod
from chattersyn.services.youtube import service as service_mod
from chattersyn.services.youtube.api import BASE_URL, YoutubeApiError
from chattersyn.services.youtube.names import YouTubeNames
from chattersyn.services.youtube.quota import Pool, next_pacific_midnight
from chattersyn.services.youtube.reader import ReadQuotaExceeded, ReadRefused, StreamEnded
from chattersyn.services.youtube.resolver import LiveChatResolver, Resolution, ResolveState
from chattersyn.services.youtube.sender import YoutubeSendError
from chattersyn.services.youtube.service import (
    QuotaPoolState,
    WatchState,
    YouTubeService,
    YoutubeState,
)


def _resolution(state, video_id="vid1", live_chat_id=None):
    return Resolution(state=state, video_id=video_id, live_chat_id=live_chat_id,
                      title="A stream", channel_title="A channel")


LIVE = _resolution(ResolveState.LIVE, live_chat_id="Cg0KC2Zvbw")
OFFLINE = _resolution(ResolveState.OFFLINE, live_chat_id=None)
UPCOMING = _resolution(ResolveState.UPCOMING, live_chat_id=None)
ENDED = _resolution(ResolveState.ENDED, live_chat_id=None)
NOT_FOUND = _resolution(ResolveState.NOT_FOUND, live_chat_id=None)

VIDEO_URL = "https://www.youtube.com/watch?v=vid1"
CHANNEL = "@somechannel"


def _rest_item(msg_id, text, kind="textMessageEvent"):
    return {
        "id": msg_id,
        "snippet": {
            "type": kind, "liveChatId": "Cg0KC2Zvbw",
            "displayMessage": text,
            **({"textMessageDetails": {"messageText": text}} if kind == "textMessageEvent" else {}),
        },
        "authorDetails": {"channelId": "UCa", "displayName": "Alice"},
    }


# -- fakes --------------------------------------------------------------------------


class FakeResolver:
    """Scripted resolve() results; the last entry repeats when exhausted.
    Logs each call's target, deep flag and (with a clock) time."""

    def __init__(self, results, clock=None):
        self.results = list(results)
        self.clock = clock
        self.calls: list[str] = []
        self.deeps: list[bool] = []
        self.times: list[dt.datetime] = []
        self.forgotten: list[str] = []

    async def resolve(self, target, *, deep=False):
        self.calls.append(target)
        self.deeps.append(deep)
        if self.clock is not None:
            self.times.append(self.clock())
        entry = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(entry, BaseException):
            raise entry
        return entry

    def forget(self, target):
        self.forgotten.append(target)


class FakeReader:
    """One script entry per read() call: (items, next_token) or an exception.

    Returns the final page token (REST shape); FakeGrpcReader re-wraps it
    with the offline_at slot the real gRPC reader returns.
    """

    script: list = []      # class-level: the service builds a new instance per cycle
    instances: list = []

    def __init__(self, *args):
        self.calls: list[tuple[str, str | None]] = []
        self.metadata: tuple = ()
        type(self).instances.append(self)
        if len(args) > 1 and callable(args[1]):
            self._get_metadata = args[1]
        else:
            self._get_metadata = None

    async def read(self, live_chat_id, page_token, on_item, stop_event=None):
        self.calls.append((live_chat_id, page_token))
        entry = type(self).script.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        items, next_token = entry
        for item in items:
            if stop_event is not None and stop_event.is_set():
                break
            on_item(item)
        return next_token


class FakeGrpcReader(FakeReader):
    script: list = []
    instances: list = []

    async def read(self, live_chat_id, page_token, on_item, stop_event=None):
        if self._get_metadata is not None:
            self.metadata = await self._get_metadata()
        token = await super().read(live_chat_id, page_token, on_item, stop_event)
        return token, None


class FakeRestReader(FakeReader):
    script: list = []
    instances: list = []


class Store:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


# -- fixtures ------------------------------------------------------------------------


@pytest.fixture
def fast(monkeypatch):
    """Every wait of the watch loop lasts ~10 ms of real time: the check
    schedule answers "10 ms after the last check" for every state."""

    def soon(kind, state, *, last_check, **_):
        return last_check + dt.timedelta(milliseconds=10)

    monkeypatch.setattr(service_mod, "next_check_at", soon)
    monkeypatch.setattr(service_mod, "RE_RESOLVE_INTERVAL", 0.01)
    monkeypatch.setattr(service_mod, "UPCOMING_RE_RESOLVE", 0.01)
    monkeypatch.setattr(service_mod, "BACKOFF_BASE", 0.01)
    monkeypatch.setattr(service_mod, "BACKOFF_CAP", 0.02)


@pytest.fixture
def fake_readers(monkeypatch):
    FakeGrpcReader.script = []
    FakeGrpcReader.instances = []
    FakeRestReader.script = []
    FakeRestReader.instances = []
    monkeypatch.setattr(service_mod, "GrpcReader", FakeGrpcReader)
    monkeypatch.setattr(service_mod, "RestReader", FakeRestReader)
    yield FakeGrpcReader, FakeRestReader


@pytest.fixture
async def service():
    services = []

    def make(api_key="", now=None):
        # The API key lives in the keyring (SecretsStore), not settings.json.
        store = Store()
        if api_key:
            store.set("youtube_api_key", api_key)
        svc = YouTubeService(Settings({}), store, **({} if now is None else {"now": now}))
        # No real keyring/network: tests set svc.tokens directly.
        svc.auth.configured = lambda: False
        services.append(svc)
        return svc

    yield make
    for svc in services:
        await svc.stop()


@pytest.fixture
def signals():
    events, messages = [], []
    conn_e = bridge.event.connect(events.append)
    conn_m = bridge.message.connect(messages.append)
    yield events, messages
    bridge.event.disconnect(conn_e)
    bridge.message.disconnect(conn_m)


async def wait_until(predicate, timeout=2.0, step=0.005):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


async def settle(rounds=20):
    """Let the watch tasks run until they block again."""
    for _ in range(rounds):
        await asyncio.sleep(0)


# -- schedule-driven time ---------------------------------------------------------------

T0 = dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.UTC)
MIN = dt.timedelta(minutes=1)


class FakeClock:
    """Injected YouTubeService clock (aware UTC); moves only when told."""

    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += dt.timedelta(seconds=seconds)


class Naps:
    """Stands in for YouTubeService._nap and logs (watch_key, due, seconds).

    Manual (default): a nap returns only when its watch is woken, so time
    moves only when the test moves the clock. auto=True: each nap advances the
    clock by its length until `until`; from then on naps block and `idle` is set.
    """

    def __init__(self, clock, *, auto=False, until=None):
        self.clock = clock
        self.auto = auto
        self.until = until
        self.log: list[tuple[str, dt.datetime, float]] = []
        self.idle = asyncio.Event()
        self.sleepers: dict[str, object] = {}  # watch_key -> the last one to nap

    async def __call__(self, watch, seconds):
        self.log.append((watch.watch_key, watch.next_check, seconds))
        self.sleepers[watch.watch_key] = watch
        if self.auto and (self.until is None or self.clock.t < self.until):
            self.clock.advance(seconds)
            await asyncio.sleep(0)
            return
        if self.auto:
            self.idle.set()
        await watch.wake.wait()

    def dues(self, key="k1"):
        return [due for k, due, _ in self.log if k == key]

    def chunks(self, key="k1"):
        return [seconds for k, _, seconds in self.log if k == key]

    def wake(self, key="k1"):
        """End the current nap of `key` (its chunk is over)."""
        self.sleepers[key].wake.set()


@pytest.fixture
def clock():
    return FakeClock()


def _scheduled(service, clock, results, *, auto=False, until=None, api_key=""):
    """A service on the fake clock with a scripted resolver and logged naps."""
    svc = service(api_key=api_key, now=clock)
    svc.resolver = FakeResolver(results, clock=clock)
    naps = Naps(clock, auto=auto, until=until)
    svc._nap = naps  # instance attribute shadows the bound method
    return svc, naps


# -- live read + reconnect ------------------------------------------------------------


async def test_live_reads_and_reconnects_with_page_token(service, signals, fake_readers, fast):
    svc = service(api_key="test-key")
    events, messages = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [
        ([_rest_item("m1", "hello from gRPC")], "page-7"),        # cycle 1: EOF w/ token
        StreamEnded("chat ended or disabled (indistinguishable)"),  # cycle 2: terminal
    ]
    svc.watch(VIDEO_URL, "k1")

    assert await wait_until(lambda: any(e.state is WatchState.UNAVAILABLE for e in events))
    assert [c[1] for c in FakeGrpcReader.instances[0].calls] == [None]
    assert [c[1] for c in FakeGrpcReader.instances[1].calls] == ["page-7"]  # resumed
    assert any(m.plain == "hello from gRPC" for m in messages)
    states = [e.state for e in events]
    assert WatchState.LIVE in states
    assert states[-1] is WatchState.UNAVAILABLE


class _NamesApi:
    """Stub channels.list for YouTubeNames in service tests."""

    def __init__(self, names):
        self.names = names
        self.calls: list[list[str]] = []
        self.quota = SimpleNamespace(exhausted=lambda pool=None: False)

    async def read_pool(self, *, signin=False):
        return Pool.SHARED

    async def channels_names(self, ids):
        self.calls.append(list(ids))
        return {cid: self.names[cid] for cid in ids if cid in self.names}


async def test_grpc_item_without_author_details_gets_display_name(
        service, signals, fake_readers, fast, monkeypatch):
    # gRPC streamList sends no authorDetails — the mapper names the author
    # after the channel id, and the service resolves the display name before
    # the message hits the bus.
    monkeypatch.setattr(names_mod, "FLUSH_DELAY", 0.0)
    svc = service(api_key="k")
    names_api = _NamesApi({"UCzzz": "Alice"})
    svc.names = YouTubeNames(names_api)
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [([{
        "id": "m1",
        "snippet": {
            "type": "textMessageEvent",
            "liveChatId": "Cg0KC2Zvbw",
            "authorChannelId": "UCzzz",
            "displayMessage": "hi",
            "textMessageDetails": {"messageText": "hi"},
        },
    }], None)]
    svc.watch(VIDEO_URL, "k")
    _, messages = signals
    await wait_until(lambda: len(messages) == 1)
    assert messages[0].author.name == "Alice"
    assert names_api.calls == [["UCzzz"]]


async def test_grpc_item_second_message_uses_cached_name(
        service, signals, fake_readers, fast, monkeypatch):
    # A resolved name is cached: the next message from the same chatter goes
    # straight through with no hold and no extra API call.
    monkeypatch.setattr(names_mod, "FLUSH_DELAY", 0.0)
    svc = service(api_key="k")
    names_api = _NamesApi({"UCzzz": "Alice"})
    svc.names = YouTubeNames(names_api)
    svc.names._cache["UCzzz"] = "Alice"
    svc.resolver = FakeResolver([LIVE])
    item = {
        "id": "m1",
        "snippet": {
            "type": "textMessageEvent",
            "liveChatId": "Cg0KC2Zvbw",
            "authorChannelId": "UCzzz",
            "displayMessage": "hi",
            "textMessageDetails": {"messageText": "hi"},
        },
    }
    FakeGrpcReader.script = [([item], None)]
    svc.watch(VIDEO_URL, "k")
    _, messages = signals
    await wait_until(lambda: len(messages) == 1)
    assert messages[0].author.name == "Alice"
    assert names_api.calls == []


async def test_chat_ended_event_stops_watch(service, signals, fake_readers, fast):
    svc = service(api_key="test-key")
    events, messages = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [
        ([_rest_item("ce1", "chat has ended", kind="chatEndedEvent")], "page-9"),
    ]
    svc.watch(VIDEO_URL, "k1")

    task = svc._watches["k1"].task
    assert await wait_until(lambda: task.done())
    assert any(m.extra.get("youtube_type") == "chatEndedEvent" for m in messages)
    assert WatchState.UNAVAILABLE not in [e.state for e in events]  # clean end, not an error


# -- resolution states -----------------------------------------------------------------


async def test_channel_target_rechecks_after_chat_unavailable(service, signals, fake_readers, fast):
    svc = service(api_key="test-key")
    events, _ = signals
    svc.resolver = FakeResolver([OFFLINE, LIVE])
    FakeGrpcReader.script = [StreamEnded("chat ended or disabled (indistinguishable)")]
    svc.watch(CHANNEL, "k1")

    assert await wait_until(lambda: len(svc.resolver.calls) >= 3)
    states = [e.state for e in events]
    # _read_until_end re-emits LIVE at the top of each read cycle.
    assert states[:4] == [WatchState.OFFLINE, WatchState.LIVE, WatchState.LIVE,
                          WatchState.UNAVAILABLE]
    assert states[4] is WatchState.LIVE  # it kept re-resolving for a new stream
    task = svc._watches["k1"].task
    assert not task.done()  # still watching
    svc.unwatch("k1")


async def test_channel_target_rechecks_after_chat_ended(service, signals, fake_readers, fast):
    svc = service(api_key="test-key")
    events, _ = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [
        ([_rest_item("ce1", "chat has ended", kind="chatEndedEvent")], "page-9"),
    ]
    svc.watch(CHANNEL, "k1")

    assert await wait_until(lambda: len(svc.resolver.calls) >= 2)
    states = [e.state for e in events]
    assert states[:4] == [WatchState.LIVE, WatchState.LIVE,
                          WatchState.ENDED, WatchState.LIVE]
    task = svc._watches["k1"].task
    assert not task.done()  # still watching for the next stream
    svc.unwatch("k1")


async def test_live_without_chat_id_reresolves(service, signals, fake_readers, fast):
    svc = service(api_key="test-key")
    events, _ = signals
    live_no_id = _resolution(ResolveState.LIVE, live_chat_id=None)
    svc.resolver = FakeResolver([live_no_id, LIVE])
    FakeGrpcReader.script = [StreamEnded("chat ended or disabled (indistinguishable)")]
    svc.watch(VIDEO_URL, "k1")

    assert await wait_until(lambda: any(e.state is WatchState.UNAVAILABLE for e in events))
    assert svc.resolver.calls == [VIDEO_URL, VIDEO_URL]
    # LIVE from the re-resolve + LIVE from the read cycle + the failure.
    assert [e.state for e in events] == [
        WatchState.LIVE, WatchState.LIVE, WatchState.LIVE, WatchState.UNAVAILABLE]


async def test_upcoming_video_rechecks_then_stops(service, signals, fake_readers, fast):
    svc = service()
    events, _ = signals
    svc.resolver = FakeResolver([UPCOMING, ENDED])
    svc.watch(VIDEO_URL, "k1")

    task = svc._watches["k1"].task
    assert await wait_until(lambda: task.done())
    assert svc.resolver.calls == [VIDEO_URL, VIDEO_URL]
    assert [e.state for e in events] == [WatchState.UPCOMING, WatchState.ENDED]


async def test_ended_video_stops_immediately(service, signals, fake_readers, fast):
    svc = service()
    events, _ = signals
    svc.resolver = FakeResolver([ENDED])
    svc.watch(VIDEO_URL, "k1")

    task = svc._watches["k1"].task
    assert await wait_until(lambda: task.done())
    assert svc.resolver.calls == [VIDEO_URL]
    assert [e.state for e in events] == [WatchState.ENDED]


async def test_not_found_channel_keeps_rechecking(service, signals, fake_readers, fast):
    svc = service()
    events, _ = signals
    svc.resolver = FakeResolver([NOT_FOUND])
    svc.watch(CHANNEL, "k1")

    assert await wait_until(lambda: sum(e.state is WatchState.NOT_FOUND for e in events) >= 2)
    svc.unwatch("k1")  # would otherwise re-check forever


# -- mine target (Stream tab) -----------------------------------------------------------


async def test_mine_offline_reresolves_until_live(service, signals, fake_readers, fast):
    svc = service(api_key="test-key")
    events, _ = signals
    svc.resolver = FakeResolver([OFFLINE, LIVE])
    FakeGrpcReader.script = [StreamEnded("chat ended or disabled (indistinguishable)")]
    svc.watch("mine:", "k1")

    assert await wait_until(lambda: len(svc.resolver.calls) >= 3)
    states = [e.state for e in events]
    # Same shape as a channel target: offline wait, live read, then a fresh
    # resolve for the next stream.
    assert states[:4] == [WatchState.OFFLINE, WatchState.LIVE, WatchState.LIVE,
                          WatchState.UNAVAILABLE]
    assert states[4] is WatchState.LIVE  # it kept re-resolving for a new stream
    assert not svc._watches["k1"].task.done()  # still watching
    svc.unwatch("k1")


async def test_mine_chat_ended_keeps_watching(service, signals, fake_readers, fast):
    svc = service(api_key="test-key")
    events, _ = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [
        ([_rest_item("ce1", "chat has ended", kind="chatEndedEvent")], "page-9"),
    ]
    svc.watch("mine:", "k1")

    assert await wait_until(lambda: len(svc.resolver.calls) >= 2)
    states = [e.state for e in events]
    assert states[:4] == [WatchState.LIVE, WatchState.LIVE,
                          WatchState.ENDED, WatchState.LIVE]
    assert not svc._watches["k1"].task.done()  # still watching for the next stream
    svc.unwatch("k1")


async def test_mine_unauthenticated_resolve_emits_auth_required(
        service, signals, fake_readers, fast):
    svc = service(api_key="test-key")  # read creds exist, but mine=true needs OAuth
    events, _ = signals
    svc.resolver = FakeResolver([YoutubeApiError(401, "authError", "API key not valid here")])
    svc.watch("mine:", "k1")

    auth_required = await wait_until(
        lambda: sum(e.state is WatchState.AUTH_REQUIRED for e in events) >= 2)
    assert auth_required
    assert events[-1].detail == "connect a Google account to watch your stream"
    svc.unwatch("k1")


async def test_unauthenticated_resolve_emits_auth_required_for_video_target(
        service, signals, fake_readers, fast):
    # Side-effect fix: a 401 during resolve no longer loops "resolving…"
    # forever — it surfaces the visible auth-required state.
    svc = service(api_key="test-key")
    events, _ = signals
    svc.resolver = FakeResolver([YoutubeApiError(401, "unauthenticated", "bad token")])
    svc.watch(VIDEO_URL, "k1")

    assert await wait_until(lambda: any(e.state is WatchState.AUTH_REQUIRED for e in events))
    svc.unwatch("k1")


async def test_mine_offline_rechecks_every_2_minutes_on_or_off_screen(service, clock):
    svc, naps = _scheduled(service, clock, [OFFLINE])
    svc.watch("mine:", "k1")
    assert await wait_until(lambda: naps.log)
    assert naps.dues()[-1] == T0 + 2 * MIN
    svc.set_on_screen("k1", False)  # YQB-19: the Stream tab keeps its rhythm off screen
    await settle()
    assert naps.dues()[-1] == T0 + 2 * MIN
    assert svc.resolver.times == [T0]


# -- error paths ------------------------------------------------------------------------


async def test_auth_required_without_credentials(service, signals, fake_readers, fast):
    svc = service()  # no API key, no tokens
    events, _ = signals
    svc.resolver = FakeResolver([LIVE])
    svc.watch(VIDEO_URL, "k1")

    assert await wait_until(lambda: any(e.state is WatchState.AUTH_REQUIRED for e in events))
    assert FakeGrpcReader.instances == [] and FakeRestReader.instances == []
    svc.unwatch("k1")


async def test_grpc_failures_fall_back_to_rest(service, signals, fake_readers, fast):
    svc = service()
    svc.tokens = SimpleNamespace(token="tok")  # REST fallback is OAuth-only
    events, messages = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [RuntimeError("boom")] * 3
    FakeRestReader.script = [
        ([_rest_item("m1", "from REST")], "page-2"),
        StreamEnded("chat ended or disabled (indistinguishable)"),
    ]
    svc.watch(VIDEO_URL, "k1")

    assert await wait_until(lambda: any(e.state is WatchState.UNAVAILABLE for e in events))
    assert len(FakeGrpcReader.instances) == 3
    assert len(FakeRestReader.instances) == 2
    assert any(m.plain == "from REST" for m in messages)


async def test_rest_token_persisted_on_chat_end(service, signals, fake_readers, fast):
    svc = service()
    svc.tokens = SimpleNamespace(token="tok")
    events, messages = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [RuntimeError("boom")] * 3
    FakeRestReader.script = [
        ([_rest_item("ce1", "chat has ended", kind="chatEndedEvent")], "page-9"),
    ]
    svc.watch(VIDEO_URL, "k1")

    task = svc._watches["k1"].task
    assert await wait_until(lambda: task.done())
    assert any(m.extra.get("youtube_type") == "chatEndedEvent" for m in messages)
    # the REST fallback's page token is kept for a mid-chat resume
    assert svc._watches["k1"].read_page_token == "page-9"


async def test_rest_quota_used_up_waits_for_the_shared_reset(
        service, signals, fake_readers, fast):
    svc = service()
    _sign_in(svc)  # REST chat reads are sign-in only
    events, messages = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [RuntimeError("boom")] * 3
    FakeRestReader.script = [YoutubeApiError(403, "quotaExceeded", "out of quota")]
    svc.watch(VIDEO_URL, "k1")

    assert await wait_until(lambda: _waits(events))
    assert svc.quota.exhausted(Pool.SHARED)
    assert _waits(events)[0].resume_at == svc.quota.resume_at(Pool.SHARED)
    assert WatchState.UNAVAILABLE not in [e.state for e in _states(events)]
    assert any("quota used up" in m.plain for m in messages)
    assert not svc._watches["k1"].task.done()  # the video tab keeps watching


# -- lifecycle ---------------------------------------------------------------------------


async def test_unwatch_cancels_task(service, signals, fake_readers, fast):
    svc = service()
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [([], None)]  # EOF-cycles forever until cancelled
    svc.watch(VIDEO_URL, "k1")
    task = svc._watches["k1"].task
    await asyncio.sleep(0.05)  # let it enter the read cycle
    svc.unwatch("k1")
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_watch_same_key_updates_target(service, signals, fake_readers, fast):
    svc = service()
    svc.resolver = FakeResolver([ENDED])
    svc.watch(VIDEO_URL, "k1")
    task = svc._watches["k1"].task
    svc.watch("https://www.youtube.com/watch?v=vid2", "k1")
    assert svc._watches["k1"].task is task
    assert svc._watches["k1"].target == "https://www.youtube.com/watch?v=vid2"
    svc.unwatch("k1")


# -- stats loop ----------------------------------------------------------------------


async def test_stats_loop_emits_live_with_fresh_viewers(monkeypatch, service, signals):
    monkeypatch.setattr(service_mod, "STREAM_STATS_INTERVAL", 0.01)
    svc = service(api_key="KEY")
    watch = service_mod._Watch(target=VIDEO_URL, watch_key="k", video_id="vid1",
                               live_chat_id="Cg0KC2Zvbw", channel_id="UC1",
                               title="A stream", channel_title="A channel")

    async def fake_videos(video_id, *, signin=False):
        return {"items": [{
            "liveStreamingDetails": {"concurrentViewers": "77",
                                     "actualStartTime": "2026-10-16T10:00:00Z"},
        }]}

    svc.api.videos = fake_videos  # instance attribute shadows the method
    task = asyncio.ensure_future(svc._stats_loop("k", watch))
    events, _ = signals
    try:
        await wait_until(lambda: any(
            e.state is WatchState.LIVE and e.viewers == 77 for e in events))
    finally:
        task.cancel()
    live = [e for e in events if e.viewers == 77][0]
    assert live.started_at is not None
    assert live.channel_title == "A channel"


async def test_stats_loop_quota_stops_gracefully(monkeypatch, service, signals):
    """A quota error kills the stats refresh only — chat reading (quota-free
    gRPC) is unaffected, so no QUOTA_EXHAUSTED emit and no crash."""
    monkeypatch.setattr(service_mod, "STREAM_STATS_INTERVAL", 0.01)
    svc = service(api_key="KEY")
    watch = service_mod._Watch(target=VIDEO_URL, watch_key="k", video_id="vid1",
                               live_chat_id="Cg0KC2Zvbw")

    async def fake_videos(video_id, *, signin=False):
        raise YoutubeApiError(403, "quotaExceeded", "quota")

    svc.api.videos = fake_videos
    task = asyncio.ensure_future(svc._stats_loop("k", watch))
    events, _ = signals
    try:
        await wait_until(lambda: task.done())
    finally:
        task.cancel()
    assert not [e for e in events if e.viewers]  # no stats emit ever


# -- api key storage -------------------------------------------------------------


async def test_api_key_reads_from_secrets_store(service):
    svc = service(api_key="KEY")
    assert svc._api_key == "KEY"
    assert svc.can_read()


async def test_legacy_settings_api_key_migrates_to_store(tmp_path):
    """A pre-keyring plain-text key in settings.json is moved into the store
    and blanked on disk at first service construction."""
    settings = Settings({"youtube": {"api_key": "legacy-key"}},
                        tmp_path / "settings.json")
    store = Store()
    svc = YouTubeService(settings, store)
    try:
        assert svc._api_key == "legacy-key"
        assert store.get("youtube_api_key") == "legacy-key"
        assert settings.get("youtube.api_key") == ""
        on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert on_disk["youtube"].get("api_key") == ""
    finally:
        await svc.stop()


# -- streaming credential (youtube-quota-budget 2.2) ---------------------------------


MINE_URL = "mine:"


async def test_yqb22_video_tab_streams_with_key_when_signed_in(
        service, signals, fake_readers, fast):
    svc = service(api_key="K")
    svc.tokens = SimpleNamespace(token="tok")
    events, _ = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [StreamEnded("chat ended or disabled (indistinguishable)")]
    svc.watch(VIDEO_URL, "k1")
    assert await wait_until(lambda: any(e.state is WatchState.UNAVAILABLE for e in events))
    assert FakeGrpcReader.instances[0].metadata == (("x-goog-api-key", "K"),)
    assert svc.quota.used_in(Pool.KEY) == 5
    assert svc.quota.used_in(Pool.SHARED) == 0


async def test_yqb24_mine_watch_streams_with_signin(service, signals, fake_readers, fast):
    svc = service(api_key="K")
    svc.tokens = SimpleNamespace(token="tok")
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [StreamEnded("done")]
    svc.watch(MINE_URL, "k1")
    assert await wait_until(lambda: FakeGrpcReader.instances)
    assert FakeGrpcReader.instances[0].metadata == (("authorization", "Bearer tok"),)
    assert svc.quota.used_in(Pool.SHARED) == 5


async def test_yqb24_stats_refresh_asks_for_signin(service, signals, fake_readers, fast,
                                                   monkeypatch):
    monkeypatch.setattr(service_mod, "STREAM_STATS_INTERVAL", 0.01)
    svc = service(api_key="K")
    svc.tokens = SimpleNamespace(token="tok")
    svc.resolver = FakeResolver([LIVE])
    calls = []

    async def videos(video_id, *, signin=False):
        calls.append(signin)
        return None

    svc.api.videos = videos
    # keep the stream open long enough for a stats tick
    class SlowGrpc(FakeGrpcReader):
        async def read(self, *a, **k):
            await asyncio.sleep(0.1)
            raise StreamEnded("done")

    monkeypatch.setattr(service_mod, "GrpcReader", SlowGrpc)
    svc.watch(MINE_URL, "k1")
    assert await wait_until(lambda: calls)
    assert calls[0] is True


async def test_yqb42_key_refused_falls_to_signin_without_blaming_key(
        service, signals, fake_readers, fast):
    svc = service(api_key="K")
    svc.tokens = SimpleNamespace(token="tok")
    events, _ = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [
        ReadRefused("not authorized", key_invalid=False),
        StreamEnded("chat ended or disabled (indistinguishable)"),
    ]
    svc.watch(VIDEO_URL, "k1")
    assert await wait_until(lambda: any(e.state is WatchState.UNAVAILABLE for e in events))
    assert FakeGrpcReader.instances[0].metadata == (("x-goog-api-key", "K"),)
    assert FakeGrpcReader.instances[1].metadata == (("authorization", "Bearer tok"),)
    assert svc.api.key_problem == ""


async def test_key_invalid_sets_problem_and_retries_with_signin(
        service, signals, fake_readers, fast):
    svc = service(api_key="K")
    svc.tokens = SimpleNamespace(token="tok")
    events, _ = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [
        ReadRefused("API_KEY_INVALID", key_invalid=True),
        StreamEnded("chat ended or disabled (indistinguishable)"),
    ]
    svc.watch(VIDEO_URL, "k1")
    assert await wait_until(lambda: any(e.state is WatchState.UNAVAILABLE for e in events))
    assert svc.api.key_problem == "API_KEY_INVALID"
    assert FakeGrpcReader.instances[1].metadata == (("authorization", "Bearer tok"),)


async def test_key_invalid_without_signin_reports_auth_required(
        service, signals, fake_readers, fast):
    svc = service(api_key="K")
    events, _ = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [ReadRefused("API_KEY_INVALID", key_invalid=True)]
    svc.watch(VIDEO_URL, "k1")
    assert await wait_until(lambda: any(e.state is WatchState.AUTH_REQUIRED for e in events))
    assert svc.api.key_problem == "API_KEY_INVALID"


async def test_yqb55_no_signin_refused_is_unavailable_and_keeps_key(
        service, signals, fake_readers, fast):
    svc = service(api_key="K")
    events, _ = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [
        ReadRefused("not authorized", key_invalid=False),
        StreamEnded("done"),
    ]
    svc.watch(VIDEO_URL, "k1")
    assert await wait_until(lambda: any(e.state is WatchState.UNAVAILABLE for e in events))
    svc.watch("https://www.youtube.com/watch?v=abcdefghijk", "k2")
    assert await wait_until(lambda: len(FakeGrpcReader.instances) >= 2)
    assert FakeGrpcReader.instances[1].metadata == (("x-goog-api-key", "K"),)
    assert svc.api.key_problem == ""


@pytest.mark.parametrize("key_invalid", [False, True])
async def test_signin_refused_is_terminal_not_a_hot_loop(
        service, signals, fake_readers, fast, key_invalid):
    svc = service()  # no key: the read goes out with the sign-in
    svc.tokens = SimpleNamespace(token="tok")
    events, _ = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [ReadRefused("SERVICE_DISABLED", key_invalid=key_invalid)] * 20
    svc.watch(VIDEO_URL, "k1")
    assert await wait_until(lambda: any(e.state is WatchState.UNAVAILABLE for e in events))
    assert len(FakeGrpcReader.instances) <= 2
    assert svc.quota.used_in(Pool.SHARED) <= 10


# -- check schedule and visibility (youtube-quota-budget 3.3) --------------------------


async def test_yqb54_leaving_screen_moves_next_check_to_last_plus_15(service, clock):
    svc, naps = _scheduled(service, clock, [OFFLINE])
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: naps.log)
    assert naps.dues()[-1] == T0 + 2 * MIN  # on screen, recently offline
    clock.advance(60)
    svc.set_on_screen("k1", False)
    await settle()
    assert naps.dues()[-1] == T0 + 15 * MIN  # last check + 15 min, not now + 15
    assert svc.resolver.times == [T0]


async def test_yqb17_selecting_a_stale_hidden_tab_checks_at_once(service, clock):
    svc, naps = _scheduled(service, clock, [OFFLINE])
    svc.watch(CHANNEL, "k1")
    svc.set_on_screen("k1", False)
    assert await wait_until(lambda: naps.log)
    assert naps.dues()[-1] == T0 + 15 * MIN
    clock.advance(5 * 60)
    svc.set_on_screen("k1", True)
    assert await wait_until(lambda: len(svc.resolver.times) == 2)
    assert svc.resolver.times == [T0, T0 + 5 * MIN]
    await settle()
    assert naps.dues()[-1] == T0 + 7 * MIN  # then the on-screen rhythm


async def test_yqb39_selecting_a_fresh_tab_makes_no_extra_check(service, clock):
    svc, naps = _scheduled(service, clock, [OFFLINE])
    svc.watch(CHANNEL, "k1")
    svc.set_on_screen("k1", False)
    assert await wait_until(lambda: naps.log)
    clock.advance(20)
    svc.set_on_screen("k1", True)
    await settle()
    assert svc.resolver.times == [T0]
    assert naps.dues()[-1] == T0 + 2 * MIN


async def test_yqb18_restoring_the_window_checks_the_selected_tab(service, clock):
    svc, naps = _scheduled(service, clock, [OFFLINE])
    svc.watch("@chanone", "k1")  # the selected tab
    svc.watch("@chantwo", "k2")  # another tab
    svc.set_on_screen("k2", False)
    assert await wait_until(lambda: naps.dues("k1") and naps.dues("k2"))
    svc.set_on_screen("k1", False)  # minimized: no tab is on screen
    svc.set_on_screen("k2", False)
    clock.advance(5 * 60)
    svc.set_on_screen("k1", True)  # restored: the selected tab is on screen again
    svc.set_on_screen("k2", False)
    assert await wait_until(lambda: svc.resolver.calls.count("@chanone") == 2)
    await settle()
    assert svc.resolver.calls.count("@chantwo") == 1
    assert naps.dues("k2")[-1] == T0 + 15 * MIN


async def test_yqb40_not_found_channel_rechecks_every_15_minutes(service, clock, signals):
    events, _ = signals
    svc, naps = _scheduled(service, clock, [NOT_FOUND], auto=True, until=T0 + 31 * MIN)
    svc.watch(CHANNEL, "k1")  # on screen: still 15 minutes
    assert await wait_until(naps.idle.is_set)
    assert svc.resolver.times == [T0, T0 + 15 * MIN, T0 + 30 * MIN]
    assert {e.state for e in events} == {WatchState.NOT_FOUND}
    assert svc.resolver.forgotten == [CHANNEL] * 3
    assert not svc._watches["k1"].task.done()  # keeps watching


async def test_yqb41_channel_found_later_deep_scans_and_reads(
        service, clock, signals, fake_readers):
    events, _ = signals
    svc, naps = _scheduled(service, clock, [NOT_FOUND, LIVE, OFFLINE], auto=True,
                           until=T0 + 30 * MIN, api_key="K")
    FakeGrpcReader.script = [StreamEnded("chat ended or disabled (indistinguishable)")]
    svc.watch(CHANNEL, "k1")
    assert await wait_until(naps.idle.is_set)
    assert svc.resolver.times[:2] == [T0, T0 + 15 * MIN]
    assert svc.resolver.deeps[:3] == [True, True, False]  # found: deep; then cheap re-checks
    assert FakeGrpcReader.instances[0].calls == [("Cg0KC2Zvbw", None)]  # reading started
    assert WatchState.LIVE in [e.state for e in events]


async def test_yqb20_hidden_live_tab_keeps_chat_and_viewer_count(
        service, signals, fake_readers, monkeypatch):
    monkeypatch.setattr(service_mod, "STREAM_STATS_INTERVAL", 0.01)
    events, messages = signals
    svc = service(api_key="K")
    svc.resolver = FakeResolver([LIVE])

    async def videos(video_id, *, signin=False):
        return {"items": [{"liveStreamingDetails": {"concurrentViewers": "77"}}]}

    svc.api.videos = videos

    class OpenStream(FakeGrpcReader):
        async def read(self, live_chat_id, page_token, on_item, stop_event=None):
            on_item(_rest_item("m1", "still here"))
            await asyncio.sleep(0.2)  # the stream stays open across stats ticks
            raise StreamEnded("done")

    monkeypatch.setattr(service_mod, "GrpcReader", OpenStream)
    svc.watch(VIDEO_URL, "k1")
    svc.set_on_screen("k1", False)
    assert await wait_until(lambda: any(m.plain == "still here" for m in messages))
    assert await wait_until(lambda: any(
        e.state is WatchState.LIVE and e.viewers == 77 for e in events))
    assert svc._watches["k1"].on_screen is False


async def test_stream_end_restarts_the_offline_rhythm(service, clock, fake_readers, monkeypatch):
    svc, naps = _scheduled(service, clock, [LIVE, OFFLINE], api_key="K")

    class TwoHourStream(FakeGrpcReader):
        async def read(self, live_chat_id, page_token, on_item, stop_event=None):
            clock.advance(2 * 3600)
            on_item(_rest_item("ce1", "chat has ended", kind="chatEndedEvent"))
            return "page-9", None

    monkeypatch.setattr(service_mod, "GrpcReader", TwoHourStream)
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: naps.log)
    # Live was seen until the end: 2-minute rhythm from then, not a catch-up check.
    assert naps.dues()[-1] == T0 + dt.timedelta(hours=2, minutes=2)
    assert svc.resolver.times == [T0]


async def test_live_without_chat_id_rechecks_after_30_s(service, clock):
    live_no_id = _resolution(ResolveState.LIVE, live_chat_id=None)
    svc, naps = _scheduled(service, clock, [live_no_id])
    svc.watch(CHANNEL, "k1")
    svc.set_on_screen("k1", False)  # this wait does not follow the visibility rhythm
    assert await wait_until(lambda: naps.log)
    assert naps.dues()[-1] == T0 + dt.timedelta(seconds=30)


async def test_wall_clock_jump_during_a_sleep_checks_within_one_chunk(service, clock):
    # asyncio.sleep runs on the monotonic clock, which stops during suspend.
    svc, naps = _scheduled(service, clock, [OFFLINE])
    svc.watch(CHANNEL, "k1")
    svc.set_on_screen("k1", False)  # next check in 15 minutes
    chunks = []

    async def suspend_once(watch, seconds):
        chunks.append(seconds)
        if len(chunks) == 1:
            clock.advance(3 * 3600)  # the machine slept: this nap ends 3 h later
            return
        await watch.wake.wait()

    svc._nap = suspend_once
    assert await wait_until(lambda: len(svc.resolver.times) == 2)
    assert chunks[0] == 60  # a 15-minute wait sleeps in chunks of at most 60 s
    assert svc.resolver.times == [T0, T0 + dt.timedelta(hours=3)]  # right after that chunk
    await settle()
    assert all(seconds <= 60 for seconds in chunks)


async def test_visibility_flapping_one_check_per_minute(service, clock):
    svc, naps = _scheduled(service, clock, [OFFLINE])
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: naps.log)
    svc.set_on_screen("k1", False)
    clock.advance(5 * 60)  # the last check is stale now
    for _ in range(20):  # 20 on/off toggles within 60 s
        clock.advance(1.5)
        svc.set_on_screen("k1", True)
        await settle()
        clock.advance(1.5)
        svc.set_on_screen("k1", False)
        await settle()
    assert len(svc.resolver.times) <= 2  # at most one extra check


async def test_crashing_check_after_coming_on_screen_waits_before_retrying(service, clock):
    # A non-API error (e.g. no network) after an at-once check must not leave
    # that request pending, or every retry would run at once again.
    svc, naps = _scheduled(service, clock, [OFFLINE, OSError("network is unreachable")])
    svc.watch(CHANNEL, "k1")
    svc.set_on_screen("k1", False)
    assert await wait_until(lambda: naps.log)
    clock.advance(5 * 60)
    svc.set_on_screen("k1", True)
    assert await wait_until(lambda: len(svc.resolver.times) == 2)
    await settle()
    assert len(svc.resolver.times) == 2
    assert naps.dues()[-1] == T0 + 6 * MIN  # the usual 60-s retry


# -- 24-hour budget of an idle channel tab (real resolver, counting fake API) -----------


def _upload(video_id, *, live="none", minute=0):
    return {
        "id": video_id,
        "snippet": {"title": "Video", "channelTitle": "Chan", "channelId": "UCchan",
                    "publishedAt": f"2026-10-01T00:{minute:02d}:00Z",
                    "liveBroadcastContent": live},
        "liveStreamingDetails": {},
    }


class CountingApi:
    """Duck-typed YouTubeApi for the real LiveChatResolver: every call costs
    1 unit and is logged (the pattern of tests/test_youtube_resolver.py)."""

    def __init__(self, ids, videos):
        self.ids = ids  # the newest uploads page (newest first)
        self.videos = videos
        self.calls: list[str] = []
        self.playlist_error = None

    async def channel_by_handle(self, handle):
        self.calls.append("channel_by_handle")
        return {"items": [{"id": "UCchan",
                           "contentDetails": {"relatedPlaylists": {"uploads": "UUchan"}}}]}

    async def playlist_items(self, playlist_id, page_token=None, max_results=50):
        self.calls.append("playlist_items")
        if self.playlist_error:
            raise self.playlist_error
        return {"items": [{"contentDetails": {"videoId": v}} for v in self.ids]}

    async def videos_batch(self, ids):
        self.calls.append("videos_batch")
        assert len(ids) <= 50
        return [self.videos[i] for i in ids if i in self.videos]


@pytest.mark.parametrize("page", ["plain", "upcoming", "no_uploads"])
async def test_idle_channel_tab_day_budget(service, clock, signals, page):
    events, _ = signals
    ids = [f"vid{i:08d}" for i in range(50)]
    videos = {v: _upload(v, minute=59 - i) for i, v in enumerate(ids)}
    if page == "upcoming":
        # Worst case: an upcoming video without a start time makes every
        # re-check also read videos.list (2 units), and still counts as offline.
        videos[ids[1]] = _upload(ids[1], live="upcoming", minute=58)
    api = CountingApi(ids, videos)
    if page == "no_uploads":
        # Uploads playlist 404 (no public uploads): every check is a lookup
        # plus the 404 (2 units), on the offline rhythm.
        api.playlist_error = YoutubeApiError(404, "playlistNotFound", "no uploads")
    svc = service(now=clock)
    svc.resolver = LiveChatResolver(api)
    naps = Naps(clock, auto=True, until=T0 + dt.timedelta(hours=24))
    svc._nap = naps
    svc.watch(CHANNEL, "k1")  # on screen all day
    assert await wait_until(naps.idle.is_set, timeout=10)
    assert {e.state for e in events} == {WatchState.OFFLINE}
    if page != "no_uploads":
        assert api.calls.count("channel_by_handle") == 1  # one deep scan at the start
    assert len(api.calls) <= 600


# -- fix round 1: rulings R9, R8a, R8b ---------------------------------------------------


async def test_channel_without_public_uploads_stays_on_the_offline_schedule(
        service, clock, signals):
    events, _ = signals
    api = CountingApi(["vid00000000"], {})
    api.playlist_error = YoutubeApiError(404, "playlistNotFound", "no uploads")
    svc = service(now=clock)
    svc.resolver = LiveChatResolver(api)
    naps = Naps(clock)
    svc._nap = naps
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: naps.log)
    assert naps.dues()[-1] == T0 + 2 * MIN  # the offline rhythm, not the 60-s error retry
    assert {e.state for e in events} == {WatchState.OFFLINE}


async def test_stream_tab_coming_on_screen_keeps_its_2_minute_rhythm(service, clock):
    svc, naps = _scheduled(service, clock, [OFFLINE])
    svc.watch("mine:", "k1")
    svc.set_on_screen("k1", False)
    assert await wait_until(lambda: naps.log)
    clock.advance(90)  # last check is stale (> 60 s), its 2-minute check is not due yet
    svc.set_on_screen("k1", True)
    await settle()
    assert svc.resolver.times == [T0]
    assert naps.dues()[-1] == T0 + 2 * MIN


async def test_not_found_channel_coming_on_screen_keeps_its_15_minute_rhythm(service, clock):
    svc, naps = _scheduled(service, clock, [NOT_FOUND])
    svc.watch(CHANNEL, "k1")
    svc.set_on_screen("k1", False)
    assert await wait_until(lambda: naps.log)
    clock.advance(5 * 60)
    svc.set_on_screen("k1", True)
    await settle()
    assert svc.resolver.times == [T0]
    assert naps.dues()[-1] == T0 + 15 * MIN


async def test_channel_found_again_restarts_the_offline_window(service, clock):
    # NOT_FOUND for hours, then found offline: the watch starts afresh, so the
    # 30-minute "recently offline" window (2-minute rhythm) counts from now.
    svc, naps = _scheduled(service, clock, [NOT_FOUND] * 12 + [OFFLINE], auto=True,
                           until=T0 + 181 * MIN)
    svc.watch(CHANNEL, "k1")
    assert await wait_until(naps.idle.is_set)
    assert svc.resolver.times[-1] == T0 + 180 * MIN
    assert svc.resolver.deeps[-1] is True  # found after NOT_FOUND: deep scan
    assert naps.dues()[-1] == T0 + 182 * MIN


# -- quota waits per pool (youtube-quota-budget 4.1) -------------------------------------

QUOTA_ERR = YoutubeApiError(403, "quotaExceeded", "out of quota")
CHAT_ID = "Cg0KC2Zvbw"


def _sign_in(svc, token="tok"):
    """A Google sign-in for both the stream metadata and the REST client."""
    svc.tokens = SimpleNamespace(token=token)

    async def access_token():
        return token

    svc.api._get_token = access_token


async def _mock_rest(svc, handler):
    """Answer the service's REST calls with `handler` (no network)."""
    await svc.api._client.aclose()
    svc.api._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL)


def _quota_response(request):
    return httpx.Response(403, json={"error": {
        "code": 403, "message": "quota",
        "errors": [{"reason": "quotaExceeded", "message": "quota"}]}}, request=request)


def _states(events):
    return [e for e in events if isinstance(e, YoutubeState)]


def _waits(events, key="k1"):
    """The QUOTA_EXHAUSTED states emitted for `key`."""
    return [e for e in _states(events)
            if e.watch_key == key and e.state is WatchState.QUOTA_EXHAUSTED]


def _pool_states(events):
    return [(e.pool, e.resume_at) for e in events if isinstance(e, QuotaPoolState)]


async def test_yqb28_quota_used_up_during_a_check_waits_for_the_reset(
        service, clock, signals):
    events, messages = signals
    svc, naps = _scheduled(service, clock, [QUOTA_ERR])
    _sign_in(svc)  # no key: the check uses the shared pool
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: _waits(events))
    await settle()
    resume = svc.quota.resume_at(Pool.SHARED)
    midnight = next_pacific_midnight(T0)
    assert midnight + MIN <= resume <= midnight + 10 * MIN
    assert [e.resume_at for e in _waits(events)] == [resume]
    assert _pool_states(events) == [(Pool.SHARED, resume)]
    assert any("quota used up" in m.plain for m in messages)
    # It waits for the reset in chunks of at most 60 s, and keeps its watch.
    assert naps.dues()[-1] == resume
    assert naps.chunks()[-1] == 60
    assert not svc._watches["k1"].task.done()
    # Coming back on screen with a stale check does not check while waiting.
    clock.advance(5 * 60)
    svc.set_on_screen("k1", False)
    svc.set_on_screen("k1", True)
    await settle()
    assert svc.resolver.times == [T0]
    assert naps.dues()[-1] == resume


async def test_yqb30_quota_used_up_while_reading_keeps_the_video_tab(
        service, clock, signals, fake_readers):
    events, _ = signals
    svc, naps = _scheduled(service, clock, [LIVE], api_key="K")
    FakeGrpcReader.script = [ReadQuotaExceeded("quota"), StreamEnded("done")]
    svc.watch(VIDEO_URL, "k1")
    assert await wait_until(lambda: _waits(events))
    await settle()
    assert svc.quota.exhausted(Pool.KEY)
    assert not svc.quota.exhausted(Pool.SHARED)
    resume = svc.quota.resume_at(Pool.KEY)
    assert _waits(events)[-1].resume_at == resume
    assert WatchState.UNAVAILABLE not in [e.state for e in _states(events)]
    assert not svc._watches["k1"].task.done()  # the video tab keeps watching
    assert len(FakeGrpcReader.instances) == 1
    # After the next midnight Pacific Time it starts again and reads.
    clock.t = resume
    naps.wake("k1")
    assert await wait_until(lambda: len(FakeGrpcReader.instances) == 2)
    assert svc.resolver.times == [T0, resume]
    assert svc.resolver.deeps[-1] is True


async def test_yqb29_after_the_reset_the_watch_checks_deep_and_reads(
        service, clock, signals, fake_readers):
    events, messages = signals
    svc, naps = _scheduled(service, clock, [OFFLINE, QUOTA_ERR, LIVE], api_key="K")
    FakeGrpcReader.script = [([_rest_item("m1", "back again")], None), StreamEnded("done")]
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: naps.dues())
    clock.advance(120)
    naps.wake("k1")  # the 2-minute check: the key's quota is used up
    assert await wait_until(lambda: _waits(events))
    await settle()
    resume = _waits(events)[0].resume_at
    assert resume == svc.quota.resume_at(Pool.KEY)
    clock.t = resume
    naps.wake("k1")  # the wait's last chunk ends
    assert await wait_until(lambda: any(m.plain == "back again" for m in messages))
    assert svc.resolver.times == [T0, T0 + 2 * MIN, resume]
    assert svc.resolver.deeps == [True, False, True]  # resumed: a deep scan
    assert _pool_states(events) == [(Pool.KEY, resume), (Pool.KEY, None)]
    assert FakeGrpcReader.instances[0].calls == [(CHAT_ID, None)]


async def test_yqb47_other_watches_on_the_pool_wait_at_once(service, clock, signals):
    events, _ = signals
    svc, naps = _scheduled(service, clock, [OFFLINE, OFFLINE, QUOTA_ERR])
    _sign_in(svc)  # no key: both tabs read with the shared pool
    svc.watch("@chanone", "k1")
    svc.watch("@chantwo", "k2")
    assert await wait_until(lambda: naps.dues("k1") and naps.dues("k2"))
    clock.advance(120)
    naps.wake("k1")  # only k1 checks, and learns the pool is used up
    assert await wait_until(lambda: _waits(events, "k2"))
    await settle()
    assert _waits(events, "k2")[0].resume_at == _waits(events, "k1")[0].resume_at
    assert svc.resolver.calls.count("@chantwo") == 1  # no call of its own
    assert naps.dues("k2")[-1] == svc.quota.resume_at(Pool.SHARED)


async def test_yqb56_tab_opened_while_its_pool_is_used_up_waits_without_a_call(
        service, clock, signals):
    events, _ = signals
    svc, naps = _scheduled(service, clock, [OFFLINE], api_key="K")
    _sign_in(svc)
    resume = svc.quota.mark_exhausted(Pool.SHARED)
    svc.watch(MINE_URL, "k1")  # A22: the Stream tab reads with the sign-in, key or not
    svc.watch(CHANNEL, "k2")  # reads with the key: its pool is fine
    assert await wait_until(lambda: _waits(events, "k1") and svc.resolver.calls)
    await settle()
    assert _waits(events, "k1")[0].resume_at == resume
    assert svc.resolver.calls == [CHANNEL]
    assert not _waits(events, "k2")


async def test_yqb27_key_used_up_waits_key_reads_and_sends_still_work(
        service, clock, signals):
    events, _ = signals
    svc, naps = _scheduled(service, clock, [OFFLINE], api_key="K")
    _sign_in(svc)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"id": "msg1"}, request=request)

    await _mock_rest(svc, handler)
    resume = svc.quota.mark_exhausted(Pool.KEY)
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: _waits(events))
    await settle()
    assert _waits(events)[0].resume_at == resume
    assert svc.resolver.calls == []  # no read moves to the sign-in
    assert await svc.send(CHAT_ID, "hello") == {"id": "msg1"}
    assert [r.headers["authorization"] for r in sent] == ["Bearer tok"]


async def test_yqb46_send_quota_pauses_the_shared_pool_but_key_reads_go_on(
        service, signals, fake_readers, fast, monkeypatch):
    events, messages = signals
    svc = service(api_key="K")
    _sign_in(svc)
    await _mock_rest(svc, _quota_response)
    svc.resolver = FakeResolver([LIVE])
    gate = asyncio.Event()

    class HeldStream(FakeGrpcReader):
        async def read(self, live_chat_id, page_token, on_item, stop_event=None):
            self.metadata = await self._get_metadata()
            if len(type(self).instances) == 1:
                await gate.wait()  # the stream is open while the send fails
                on_item(_rest_item("m1", "still reading"))
                return "page-1", None
            raise StreamEnded("done")

    monkeypatch.setattr(service_mod, "GrpcReader", HeldStream)
    svc.watch(VIDEO_URL, "k1")
    assert await wait_until(lambda: FakeGrpcReader.instances)
    with pytest.raises(YoutubeSendError):
        await svc.send(CHAT_ID, "hello")
    resume = svc.quota.resume_at(Pool.SHARED)
    assert resume is not None
    assert _pool_states(events) == [(Pool.SHARED, resume)]
    gate.set()
    assert await wait_until(lambda: len(FakeGrpcReader.instances) == 2)
    assert any(m.plain == "still reading" for m in messages)
    assert FakeGrpcReader.instances[1].metadata == (("x-goog-api-key", "K"),)
    assert not _waits(events)
    assert not svc.quota.exhausted(Pool.KEY)


async def test_pool_reset_is_announced_once_and_wakes_waiting_watches(
        service, clock, signals):
    events, _ = signals
    svc, naps = _scheduled(service, clock, [OFFLINE])
    _sign_in(svc)
    resume = svc.quota.mark_exhausted(Pool.SHARED)
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: naps.dues("quota:shared") and naps.dues("k1"))
    assert naps.dues("quota:shared")[-1] == resume == naps.dues("k1")[-1]
    clock.t = resume
    naps.wake("quota:shared")  # only the pool's own timer: its last chunk ends
    assert await wait_until(lambda: svc.resolver.times == [resume])
    await settle()
    assert _pool_states(events) == [(Pool.SHARED, resume), (Pool.SHARED, None)]
    assert svc.resolver.deeps == [True]


async def test_key_entered_during_a_shared_wait_checks_with_it_at_once(service, clock, signals):
    # The sign-in's pool is used up; the user adds an API key in Settings: the
    # waiting watch leaves its wait without waiting for the reset and checks
    # (deep) with the key.
    events, _ = signals
    svc = service(now=clock)
    naps = Naps(clock)
    svc._nap = naps
    _sign_in(svc)  # no key: the watch's calls use the shared pool
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"items": []}, request=request)

    await _mock_rest(svc, handler)
    resume = svc.quota.mark_exhausted(Pool.SHARED)
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: _waits(events) and naps.dues("k1"))
    await settle()
    assert naps.dues("k1")[-1] == resume
    assert requests == []
    svc.set_api_key("K")
    assert await wait_until(lambda: requests)
    assert clock() < resume  # it did not wait for the reset
    assert requests[0].url.params["key"] == "K"
    assert "authorization" not in requests[0].headers
    assert svc.quota.exhausted(Pool.SHARED)
    assert _pool_states(events) == [(Pool.SHARED, resume)]  # no reset announced


async def test_visibility_wake_keeps_a_wait_whose_pool_is_still_used_up(
        service, clock, signals):
    events, _ = signals
    svc, naps = _scheduled(service, clock, [OFFLINE])
    _sign_in(svc)
    resume = svc.quota.mark_exhausted(Pool.SHARED)
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: _waits(events) and naps.dues("k1"))
    await settle()
    clock.advance(5 * 60)
    svc.set_on_screen("k1", False)
    await settle()
    svc.set_on_screen("k1", True)
    await settle()
    assert svc.resolver.calls == []
    assert len(_waits(events)) == 1  # the same wait goes on
    assert naps.dues("k1")[-1] == resume
    assert not svc._watches["k1"].task.done()


async def test_signin_fallback_is_for_that_chat_only(service, clock, signals, fake_readers):
    # R11: a members-only chat read with the sign-in does not move the channel
    # tab's later checks and streams off the key.
    events, _ = signals
    chat2 = _resolution(ResolveState.LIVE, video_id="vid2", live_chat_id="chat2")
    svc, naps = _scheduled(service, clock, [LIVE, chat2], api_key="K")
    _sign_in(svc)
    FakeGrpcReader.script = [
        ReadRefused("not authorized", key_invalid=False),  # members-only: key refused
        ([_rest_item("ce1", "chat has ended", kind="chatEndedEvent")], "page-9"),
        StreamEnded("done"),
    ]
    svc.watch(CHANNEL, "k1")
    assert await wait_until(lambda: naps.dues())
    assert FakeGrpcReader.instances[1].metadata == (("authorization", "Bearer tok"),)
    svc.quota.mark_exhausted(Pool.SHARED)  # only the sign-in's pool is used up
    await settle()
    assert not _waits(events)  # the channel's checks use the key: not blocked
    clock.advance(120)
    naps.wake("k1")
    assert await wait_until(lambda: len(FakeGrpcReader.instances) == 3)
    assert svc.resolver.times == [T0, T0 + 2 * MIN]
    assert FakeGrpcReader.instances[2].metadata == (("x-goog-api-key", "K"),)
    assert FakeGrpcReader.instances[2].calls == [("chat2", None)]
    assert not _waits(events)
