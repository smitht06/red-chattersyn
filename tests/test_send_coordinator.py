"""SendCoordinator: dual-send exactly-once, truncation semantics, the
"Send to Twitch only" offer, retries, queues, echo adoption."""

import asyncio

import pytest

from chattersyn.config.settings import Settings
from chattersyn.core.bus import bridge
from chattersyn.models.message import MessageFlags, Platform
from chattersyn.services.send import (
    MODE_BLOCK,
    MODE_HIGHLIGHT,
    MODE_SILENT,
    QUEUE_SIZE,
    SendCoordinator,
    SendTargets,
)
from chattersyn.services.twitch.irc import NotAuthenticated, NotConnected
from chattersyn.services.youtube.sender import YoutubeSendError


class FakeTwitch:
    def __init__(self, fail=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.fail = fail
        self.gate: asyncio.Event | None = None

    async def __call__(self, channel, text, **kwargs):
        self.calls.append((channel, text, kwargs))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return kwargs.get("optimistic")


class FakeYouTube:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.responses: list = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, live_chat_id, text):
        self.calls.append((live_chat_id, text))
        if self.gate is not None:
            await self.gate.wait()
        response = self.responses.pop(0) if self.responses else {"id": f"yt-{len(self.calls)}"}
        if isinstance(response, Exception):
            raise response
        return response


def _settings(mode=MODE_HIGHLIGHT):
    return Settings({"send": {"truncate_mode": mode}})


@pytest.fixture
def results():
    collected = []
    connection = bridge.send_result.connect(collected.append)
    yield collected
    bridge.send_result.disconnect(connection)


@pytest.fixture
async def coordinator(results):
    coordinators = []

    async def make(twitch=None, youtube=None, settings=None):
        coordinator = SendCoordinator(
            twitch or FakeTwitch(), youtube or FakeYouTube(), settings or _settings(),
        )
        coordinator.start()
        coordinators.append(coordinator)
        return coordinator

    yield make
    for coordinator in coordinators:
        await coordinator.stop()


async def wait_until(predicate, timeout=2.0, step=0.005):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def _states(results, platform):
    return [r.state for r in results if r.platform is platform]


def _final(results, platform):
    return next((r for r in reversed(results)
                 if r.platform is platform and r.state in ("sent", "failed",
                                                           "validation_failed", "cancelled")), None)


DUAL = SendTargets(twitch_channel="forsen", youtube_live_chat_id="Cg0KC2Zvbw")


# -- dual send ---------------------------------------------------------------------------


async def test_dual_send_calls_both_exactly_once(coordinator, results):
    twitch, youtube = FakeTwitch(), FakeYouTube()
    send = await coordinator(twitch, youtube)

    optimistic = send.send("tab-1", "hello world", DUAL)
    assert set(optimistic) == {Platform.TWITCH, Platform.YOUTUBE}

    assert await wait_until(lambda: _states(results, Platform.TWITCH)[-1:] == ["sent"]
                            and _states(results, Platform.YOUTUBE)[-1:] == ["sent"])

    assert [c[0] for c in twitch.calls] == ["forsen"]
    assert [c[1] for c in twitch.calls] == ["hello world"]
    assert [c[0] for c in youtube.calls] == ["Cg0KC2Zvbw"]
    assert [c[1] for c in youtube.calls] == ["hello world"]

    # optimistic rows: rendered immediately with a "You" author fallback
    tw_msg = optimistic[Platform.TWITCH]
    assert tw_msg.plain == "hello world"
    assert tw_msg.channel_id == "forsen"
    assert tw_msg.extra["self"] is True


async def test_twitch_failure_emits_failed_result(coordinator, results):
    send = await coordinator(FakeTwitch(fail=NotConnected("offline")), FakeYouTube())
    optimistic = send.send("tab-1", "hello", DUAL)
    assert await wait_until(lambda: _final(results, Platform.TWITCH) is not None)
    failed = _final(results, Platform.TWITCH)
    assert failed.state == "failed"
    assert "offline" in failed.error
    assert failed.message_id == optimistic[Platform.TWITCH].id  # UI can mark it deleted
    youtube_final = _final(results, Platform.YOUTUBE)
    assert youtube_final is not None and youtube_final.state == "sent"  # unaffected


async def test_youtube_not_authenticated_fails_youtube_side(coordinator, results):
    youtube = FakeYouTube()
    send = await coordinator(FakeTwitch(), youtube)
    youtube.responses = [YoutubeSendError("auth_required", "no Google account connected")]
    optimistic = send.send("tab-1", "hello", DUAL)
    assert await wait_until(lambda: _final(results, Platform.YOUTUBE) is not None)
    failed = _final(results, Platform.YOUTUBE)
    assert failed.state == "failed"
    assert failed.error == "no Google account connected"
    assert failed.message_id == optimistic[Platform.YOUTUBE].id


async def test_twitch_not_authenticated_maps_error(coordinator, results):
    send = await coordinator(FakeTwitch(fail=NotAuthenticated("anonymous")), FakeYouTube())
    send.send("tab-1", "hello", DUAL)
    assert await wait_until(lambda: _final(results, Platform.TWITCH) is not None)
    assert "anonymous" in _final(results, Platform.TWITCH).error


# -- truncation ---------------------------------------------------------------------------


async def test_dual_truncates_to_200_with_indicator(coordinator, results):
    twitch, youtube = FakeTwitch(), FakeYouTube()
    send = await coordinator(twitch, youtube)
    text = "x" * 250
    optimistic = send.send("tab-1", text, DUAL)
    assert len(optimistic[Platform.TWITCH].plain) == 200
    assert len(optimistic[Platform.YOUTUBE].plain) == 200
    assert optimistic[Platform.TWITCH].extra["truncated"] is True
    assert optimistic[Platform.YOUTUBE].extra["truncated"] is True
    assert await wait_until(lambda: _final(results, Platform.TWITCH) is not None)
    assert _final(results, Platform.TWITCH).truncated
    assert len(twitch.calls[0][1]) == 200
    assert len(youtube.calls[0][1]) == 200


async def test_dual_truncation_silent_mode(coordinator, results):
    send = await coordinator(FakeTwitch(), FakeYouTube(), _settings(MODE_SILENT))
    optimistic = send.send("tab-1", "y" * 250, DUAL)
    assert len(optimistic[Platform.TWITCH].plain) == 200
    assert optimistic[Platform.TWITCH].extra.get("truncated") is None


async def test_dual_block_mode_refuses(coordinator, results):
    twitch, youtube = FakeTwitch(), FakeYouTube()
    send = await coordinator(twitch, youtube, _settings(MODE_BLOCK))
    optimistic = send.send("tab-1", "z" * 250, DUAL)
    assert optimistic == {}
    assert len(results) == 2  # validation_failed for both platforms
    assert all(r.state == "validation_failed" for r in results)
    assert not twitch.calls and not youtube.calls


async def test_single_platform_uses_own_limit(coordinator, results):
    twitch, youtube = FakeTwitch(), FakeYouTube()
    send = await coordinator(twitch, youtube)
    targets = SendTargets(twitch_channel="forsen", youtube_live_chat_id="Cg0KC2Zvbw",
                          youtube_enabled=False)
    optimistic = send.send("tab-1", "w" * 300, targets)
    assert len(optimistic[Platform.TWITCH].plain) == 300  # 500-limit untouched
    assert Platform.YOUTUBE not in optimistic


async def test_youtube_only_send(coordinator, results):
    twitch, youtube = FakeTwitch(), FakeYouTube()
    send = await coordinator(twitch, youtube)
    targets = SendTargets(twitch_channel="forsen", youtube_live_chat_id="Cg0KC2Zvbw",
                          twitch_enabled=False)
    optimistic = send.send("tab-1", "hi youtube", targets)
    assert set(optimistic) == {Platform.YOUTUBE}
    assert await wait_until(lambda: _final(results, Platform.YOUTUBE) is not None)
    assert not twitch.calls
    assert youtube.calls == [("Cg0KC2Zvbw", "hi youtube")]


# -- the offer: never silently drop a platform --------------------------------------------


async def test_url_blocked_offers_twitch_only(coordinator, results):
    twitch, youtube = FakeTwitch(), FakeYouTube()
    send = await coordinator(twitch, youtube)
    optimistic = send.send("tab-1", "check https://example.com", DUAL)
    assert optimistic == {}  # nothing sent yet — the offer is pending
    assert not twitch.calls and not youtube.calls

    failed = _final(results, Platform.YOUTUBE)
    assert failed.state == "validation_failed"
    assert "URL" in failed.error
    assert failed.offer is Platform.TWITCH
    assert "Twitch" in failed.offer_label

    # user clicks "Send to Twitch only"
    msg = send.send_offer(failed.key, Platform.TWITCH)
    assert msg is not None and msg.platform is Platform.TWITCH
    assert await wait_until(lambda: _final(results, Platform.TWITCH) is not None)
    assert _final(results, Platform.TWITCH).state == "sent"
    assert [(c[0], c[1]) for c in twitch.calls] == [("forsen", "check https://example.com")]
    assert not youtube.calls


async def test_discarded_offer_cannot_be_accepted(coordinator, results):
    send = await coordinator(FakeTwitch(), FakeYouTube())
    send.send("tab-1", "check https://example.com", DUAL)
    failed = _final(results, Platform.YOUTUBE)
    assert failed.offer is Platform.TWITCH

    send.discard_request(failed.key)  # banner hidden without accepting
    assert send.send_offer(failed.key, Platform.TWITCH) is None
    assert not any(r.state == "sent" for r in results)


async def test_url_blocked_without_other_platform_has_no_offer(coordinator, results):
    send = await coordinator(FakeTwitch(), FakeYouTube())
    targets = SendTargets(twitch_channel="forsen", youtube_live_chat_id="Cg0KC2Zvbw",
                          twitch_enabled=False)
    send.send("tab-1", "check https://example.com", targets)
    failed = _final(results, Platform.YOUTUBE)
    assert failed.state == "validation_failed"
    assert failed.offer is None


async def test_twitch_me_strips_prefix_for_youtube(coordinator, results):
    twitch, youtube = FakeTwitch(), FakeYouTube()
    send = await coordinator(twitch, youtube)
    optimistic = send.send("tab-1", "/me dances", DUAL)
    tw = optimistic[Platform.TWITCH]
    assert tw.flags & MessageFlags.ACTION
    assert tw.plain == "dances"
    yt = optimistic[Platform.YOUTUBE]
    assert not yt.flags & MessageFlags.ACTION
    assert yt.plain == "dances"  # no /me markup leaked to YouTube
    assert await wait_until(lambda: _final(results, Platform.YOUTUBE) is not None)
    assert youtube.calls[0][1] == "dances"
    assert twitch.calls[0][1] == "dances"
    assert twitch.calls[0][2]["action"] is True  # ACTION survives to the wire


async def test_reply_parent_forwarded(coordinator, results):
    twitch, youtube = FakeTwitch(), FakeYouTube()
    send = await coordinator(twitch, youtube)
    send.send("tab-1", "replying", DUAL,
              reply_parent_id="parent-msg-1", reply_parent_login="someone")
    assert await wait_until(lambda: _final(results, Platform.TWITCH) is not None)
    kwargs = twitch.calls[0][2]
    assert kwargs["reply_parent_id"] == "parent-msg-1"
    assert kwargs["reply_parent_login"] == "someone"


# -- retries --------------------------------------------------------------------------------


async def test_youtube_rate_limit_retries_then_succeeds(coordinator, results, monkeypatch):
    monkeypatch.setattr("chattersyn.services.send.RETRY_DELAYS", (0.01, 0.01, 0.01))
    youtube = FakeYouTube()
    send = await coordinator(FakeTwitch(), youtube)
    youtube.responses = [
        YoutubeSendError("rate_limited", "slow down", retryable=True),
        {"id": "yt-ok"},
    ]
    optimistic = send.send("tab-1", "hello", DUAL)
    assert await wait_until(lambda: _final(results, Platform.YOUTUBE) is not None)
    states = _states(results, Platform.YOUTUBE)
    assert "retrying" in states
    final = _final(results, Platform.YOUTUBE)
    assert final.state == "sent"
    assert len(youtube.calls) == 2  # exactly one retry, no more
    retrying = next(r for r in results if r.state == "retrying")
    assert retrying.retry_after == 0.01
    # the optimistic row got the server id adopted
    assert optimistic[Platform.YOUTUBE].id == "yt-ok"


async def test_youtube_rate_limit_exhausted_fails(coordinator, results, monkeypatch):
    monkeypatch.setattr("chattersyn.services.send.RETRY_DELAYS", (0.01, 0.01, 0.01))
    youtube = FakeYouTube()
    send = await coordinator(FakeTwitch(), youtube)
    youtube.responses = [
        YoutubeSendError("rate_limited", "slow down", retryable=True) for _ in range(5)
    ]
    send.send("tab-1", "hello", DUAL)
    assert await wait_until(lambda: _final(results, Platform.YOUTUBE) is not None)
    final = _final(results, Platform.YOUTUBE)
    assert final.state == "failed"
    assert len(youtube.calls) == 4  # initial + 3 retries


async def test_non_retryable_youtube_error_fails_immediately(coordinator, results):
    youtube = FakeYouTube()
    send = await coordinator(FakeTwitch(), youtube)
    youtube.responses = [YoutubeSendError("chat_ended", "the live chat has ended")]
    send.send("tab-1", "hello", DUAL)
    assert await wait_until(lambda: _final(results, Platform.YOUTUBE) is not None)
    assert _final(results, Platform.YOUTUBE).state == "failed"
    assert len(youtube.calls) == 1


# -- queue + cancel ---------------------------------------------------------------------------


async def test_queue_full_drops_with_error(coordinator, results):
    twitch = FakeTwitch()
    twitch.gate = asyncio.Event()
    send = await coordinator(twitch, FakeYouTube())
    targets = SendTargets(twitch_channel="forsen", youtube_live_chat_id="Cg0KC2Zvbw",
                          youtube_enabled=False)
    send.send("tab-1", "first", targets)  # occupies the worker
    assert await wait_until(lambda: len(twitch.calls) == 1)  # worker gated on "first"
    for i in range(QUEUE_SIZE):
        send.send("tab-1", f"fill {i}", targets)
    overflow = send.send("tab-1", "overflow", targets)
    assert Platform.TWITCH not in overflow  # dropped at the door
    failed = _final(results, Platform.TWITCH)
    assert failed is not None and failed.state == "failed"
    assert "queue is full" in failed.error
    twitch.gate.set()
    assert await wait_until(lambda: len(twitch.calls) == QUEUE_SIZE + 1)


async def test_stop_with_full_queue_is_clean(coordinator, results):
    twitch = FakeTwitch()
    twitch.gate = asyncio.Event()
    send = await coordinator(twitch, FakeYouTube())
    targets = SendTargets(twitch_channel="forsen", youtube_live_chat_id="Cg0KC2Zvbw",
                          youtube_enabled=False)
    send.send("tab-1", "first", targets)
    assert await wait_until(lambda: len(twitch.calls) == 1)  # worker gated
    for i in range(QUEUE_SIZE):
        send.send("tab-1", f"fill {i}", targets)
    await send.stop()  # the sentinel cannot enter the full queue — must not raise
    assert any(r.state == "sent" for r in results) is False  # cancelled, not sent


async def test_cancel_queued_job(coordinator, results):
    twitch = FakeTwitch()
    twitch.gate = asyncio.Event()
    send = await coordinator(twitch, FakeYouTube())
    targets = SendTargets(twitch_channel="forsen", youtube_live_chat_id="Cg0KC2Zvbw",
                          youtube_enabled=False)
    send.send("tab-1", "first", targets)
    second = send.send("tab-1", "second", targets)
    assert await wait_until(lambda: len(results) >= 1)  # worker picked up "first"

    queued = next((r for r in results if r.state == "queued" and r.text == "second"), None)
    assert queued is not None
    send.cancel(queued.key, Platform.TWITCH)
    assert any(r.state == "cancelled" and r.text == "second" for r in results)

    twitch.gate.set()
    # wait for "first" to actually complete (a "cancelled" result alone isn't
    # proof the worker got there — it may not have been scheduled yet)
    assert await wait_until(lambda: any(r.state == "sent" for r in results))
    assert [c[1] for c in twitch.calls] == ["first"]  # "second" never went out
    assert second[Platform.TWITCH].id  # UI marks this optimistic row deleted


# -- echo adoption ---------------------------------------------------------------------------


async def test_note_echo_adopts_early_when_reader_wins_the_race(coordinator, results):
    from chattersyn.models.message import Message

    youtube = FakeYouTube()
    youtube.gate = asyncio.Event()
    youtube.responses = [{"id": "yt-ok"}]
    send = await coordinator(FakeTwitch(), youtube)
    optimistic = send.send("tab-1", "hello", DUAL)
    assert await wait_until(lambda: len(youtube.calls) == 1)  # insert in flight

    echo = Message.text(
        Platform.YOUTUBE, "Cg0KC2Zvbw", None, "hello", id="yt-ok",
    )
    assert send.note_echo(echo) is True  # deduped by text — id adopted early
    assert optimistic[Platform.YOUTUBE].id == "yt-ok"
    assert "pending" not in optimistic[Platform.YOUTUBE].extra

    youtube.gate.set()
    assert await wait_until(lambda: _final(results, Platform.YOUTUBE) is not None)
    assert _final(results, Platform.YOUTUBE).state == "sent"
    assert optimistic[Platform.YOUTUBE].id == "yt-ok"


async def test_note_echo_dedupes_adopted_ids(coordinator, results):
    from chattersyn.models.message import Message

    send = await coordinator(FakeTwitch(), FakeYouTube())
    optimistic = send.send("tab-1", "hello", DUAL)
    assert await wait_until(lambda: _final(results, Platform.YOUTUBE) is not None)

    echo = Message.text(
        Platform.YOUTUBE, "Cg0KC2Zvbw", None, "hello",
        id=optimistic[Platform.YOUTUBE].id,  # now the adopted server id
    )
    assert send.note_echo(echo) is True   # drop it — already shown optimistically
    assert send.note_echo(echo) is False  # second delivery: registry consumed

    other = Message.text(
        Platform.YOUTUBE, "Cg0KC2Zvbw", None, "hi", id="someone-elses-msg",
    )
    assert send.note_echo(other) is False
    # Twitch messages never hit the echo registry (IRC dedupes via nonce)
    twitch_msg = Message.text(
        Platform.TWITCH, "forsen", None, "hi", id="tw-whatever",
    )
    assert send.note_echo(twitch_msg) is False


# -- degenerate inputs -----------------------------------------------------------------------


async def test_empty_text_sends_nothing(coordinator, results):
    send = await coordinator(FakeTwitch(), FakeYouTube())
    assert send.send("tab-1", "   ", DUAL) == {}
    assert results == []


async def test_all_platforms_disabled_sends_nothing(coordinator, results):
    send = await coordinator(FakeTwitch(), FakeYouTube())
    targets = SendTargets(twitch_channel="forsen", youtube_live_chat_id="Cg0KC2Zvbw",
                          twitch_enabled=False, youtube_enabled=False)
    assert send.send("tab-1", "hello", targets) == {}
