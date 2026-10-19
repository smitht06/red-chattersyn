"""YouTubeNames: batched channels.list display-name resolution for gRPC chat
authors that only carry a channel id."""

import asyncio

import pytest

import chattersyn.services.youtube.names as names_mod
from chattersyn.core.bus import bridge
from chattersyn.models.message import Author, Message, Platform
from chattersyn.services.youtube.names import YouTubeNames


class FakeQuota:
    def __init__(self, exhausted=False):
        self._exhausted = exhausted

    def exhausted(self):
        return self._exhausted


class FakeApi:
    def __init__(self, names=None, error=None, delay=0.0, exhausted=False):
        self.names = names or {}
        self.error = error
        self.delay = delay
        self.calls: list[list[str]] = []
        self.quota = FakeQuota(exhausted)

    async def channels_names(self, ids):
        self.calls.append(list(ids))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return {cid: self.names[cid] for cid in ids if cid in self.names}


def _msg(cid, mid=None):
    return Message.text(
        Platform.YOUTUBE, "Cg0KC2Zvbw",
        Author(name=cid, login=cid.lower(), user_id=cid),
        "hi", id=mid or f"m-{cid}")


@pytest.fixture
def listener():
    messages = []
    conn = bridge.message.connect(messages.append)
    yield messages
    bridge.message.disconnect(conn)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(names_mod, "FLUSH_DELAY", 0.0)


async def wait_until(predicate, timeout=2.0, step=0.005):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not met in time")


async def test_cached_hit_and_miss():
    names = YouTubeNames(FakeApi())
    assert names.cached("UC1") is None
    names._cache["UC1"] = "Alice"
    assert names.cached("UC1") == "Alice"


async def test_hold_resolves_names_and_emits(fast, listener):
    api = FakeApi(names={"UC1": "Alice", "UC2": "Bob"})
    names = YouTubeNames(api)
    names.hold(_msg("UC1", "m1"))
    names.hold(_msg("UC2", "m2"))
    await wait_until(lambda: len(listener) == 2)
    assert [m.author.name for m in listener] == ["Alice", "Bob"]
    # one batched call for both ids
    assert api.calls == [["UC1", "UC2"]]
    assert names.cached("UC1") == "Alice"
    await names.close()


async def test_hold_chunks_above_batch_limit(fast, listener, monkeypatch):
    monkeypatch.setattr(names_mod, "BATCH", 2)
    api = FakeApi(names={f"UC{i}": f"User{i}" for i in range(5)})
    names = YouTubeNames(api)
    for i in range(5):
        names.hold(_msg(f"UC{i}", f"m{i}"))
    await wait_until(lambda: len(listener) == 5)
    assert sorted(m.author.name for m in listener) == [f"User{i}" for i in range(5)]
    assert [len(call) for call in api.calls] == [2, 2, 1]
    await names.close()


async def test_fetch_failure_emits_with_channel_id(fast, listener):
    api = FakeApi(error=RuntimeError("boom"))
    names = YouTubeNames(api)
    names.hold(_msg("UC1", "m1"))
    await wait_until(lambda: len(listener) == 1)
    assert listener[0].author.name == "UC1"  # fallback: id stays the name
    await names.close()


async def test_quota_exhausted_skips_fetch(fast, listener):
    api = FakeApi(exhausted=True)
    names = YouTubeNames(api)
    names.hold(_msg("UC1", "m1"))
    await wait_until(lambda: len(listener) == 1)
    assert listener[0].author.name == "UC1"
    assert api.calls == []
    await names.close()


async def test_holds_during_flush_get_a_second_flush(fast, listener):
    api = FakeApi(names={"UC1": "Alice", "UC2": "Bob"}, delay=0.05)
    names = YouTubeNames(api)
    names.hold(_msg("UC1", "m1"))
    await asyncio.sleep(0.01)  # flush is now awaiting the slow fetch
    names.hold(_msg("UC2", "m2"))  # lands in fresh pending; no task is free yet
    await wait_until(lambda: len(listener) == 2)
    assert sorted(m.author.name for m in listener) == ["Alice", "Bob"]
    # the second flush refetches only the still-uncached id
    assert api.calls == [["UC1"], ["UC2"]]
    await names.close()


async def test_close_drops_pending_flush(listener):
    names = YouTubeNames(FakeApi())
    names.hold(_msg("UC1", "m1"))
    await names.close()
    assert names._task is None
    assert listener == []
