"""YouTubeService watch loop: resolution states, reconnect token, chat end,
REST fallback after gRPC failures, quota terminal state, cancellation."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from chattersyn.config.settings import Settings
from chattersyn.core.bus import bridge
from chattersyn.services.youtube import service as service_mod
from chattersyn.services.youtube.api import YoutubeApiError
from chattersyn.services.youtube.reader import StreamEnded
from chattersyn.services.youtube.resolver import Resolution, ResolveState
from chattersyn.services.youtube.service import WatchState, YouTubeService


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
    """Scripted resolve() results; the last entry repeats when exhausted."""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[str] = []

    async def resolve(self, target):
        self.calls.append(target)
        entry = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(entry, BaseException):
            raise entry
        return entry


class FakeReader:
    """One script entry per read() call: (items, next_token) or an exception.

    Returns the final page token (REST shape); FakeGrpcReader re-wraps it
    with the offline_at slot the real gRPC reader returns.
    """

    script: list = []      # class-level: the service builds a new instance per cycle
    instances: list = []

    def __init__(self, *args):
        self.calls: list[tuple[str, str | None]] = []
        type(self).instances.append(self)

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

    def make(api_key=""):
        # The API key lives in the keyring (SecretsStore), not settings.json.
        store = Store()
        if api_key:
            store.set("youtube_api_key", api_key)
        svc = YouTubeService(Settings({}), store)
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


# -- error paths ------------------------------------------------------------------------


async def test_resolve_quota_exhausted_is_terminal(service, signals, fake_readers, fast):
    svc = service()
    events, messages = signals
    svc.resolver = FakeResolver([YoutubeApiError(403, "quotaExceeded", "out of quota")])
    svc.watch(VIDEO_URL, "k1")

    task = svc._watches["k1"].task
    assert await wait_until(lambda: task.done())
    assert events[-1].state is WatchState.QUOTA_EXHAUSTED
    assert any("quota exhausted" in m.plain for m in messages)


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


async def test_rest_quota_exhausted_marks_unavailable(service, signals, fake_readers, fast):
    svc = service()
    svc.tokens = SimpleNamespace(token="tok")
    events, messages = signals
    svc.resolver = FakeResolver([LIVE])
    FakeGrpcReader.script = [RuntimeError("boom")] * 3
    FakeRestReader.script = [YoutubeApiError(403, "quotaExceeded", "out of quota")]
    svc.watch(VIDEO_URL, "k1")

    assert await wait_until(lambda: any(e.state is WatchState.UNAVAILABLE for e in events))
    assert any("quota exhausted" in m.plain for m in messages)


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

    async def fake_videos(video_id):
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

    async def fake_videos(video_id):
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
