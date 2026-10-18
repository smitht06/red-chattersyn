"""LiveChatResolver: target parsing, video states, channel uploads scanning."""

import datetime as dt

import httpx
import pytest

from chattersyn.services.youtube.api import QuotaTracker, YouTubeApi
from chattersyn.services.youtube.resolver import (
    BATCH,
    MAX_PLAYLIST_PAGES,
    LiveChatResolver,
    ResolveState,
    parse_target,
)


def _client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://www.googleapis.com/youtube/v3")


def _api(handler):
    return YouTubeApi(lambda: _token(), QuotaTracker(), client=_client(handler))


async def _token():
    return "tok"


def _video(video_id, live="none", chat_id=None, title="Video", channel="Chan",
           channel_id="UCchan", published="2026-10-01T00:00:00Z"):
    video = {
        "id": video_id,
        "snippet": {
            "title": title, "channelTitle": channel, "channelId": channel_id,
            "publishedAt": published, "liveBroadcastContent": live,
        },
        "liveStreamingDetails": {},
    }
    if chat_id:
        video["liveStreamingDetails"]["activeLiveChatId"] = chat_id
    return video


def _channel(uploads="UUuploads"):
    return {
        "items": [{
            "id": "UCchannel",
            "contentDetails": {"relatedPlaylists": {"uploads": uploads}},
        }],
    }


# -- parse_target ---------------------------------------------------------------------


@pytest.mark.parametrize("target,expected", [
    ("dQw4w9WgXcQ", ("video", "dQw4w9WgXcQ")),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ("video", "dQw4w9WgXcQ")),
    ("https://youtu.be/dQw4w9WgXcQ?t=10", ("video", "dQw4w9WgXcQ")),
    ("https://www.youtube.com/live/abcDEF12345", ("video", "abcDEF12345")),
    ("https://www.youtube.com/shorts/abcDEF12345", ("video", "abcDEF12345")),
    ("@forsen", ("handle", "forsen")),
    ("forsen", ("handle", "forsen")),
    ("UC" + "a" * 22, ("channel", "UC" + "a" * 22)),
])
def test_parse_target(target, expected):
    assert parse_target(target) == expected


@pytest.mark.parametrize("target", [
    "", "  ", "not a target", "https://example.com/x", "@", "UCtoolong" * 5,
    "ab",  # too short for a handle
])
def test_parse_target_invalid(target):
    assert parse_target(target) is None


# -- video states ----------------------------------------------------------------------


async def test_live_video_resolves():
    def handler(request):
        assert request.url.params["id"] == "dQw4w9WgXcQ"
        return httpx.Response(200, json={
            "items": [_video("dQw4w9WgXcQ", live="live", chat_id="Cg0KC2Zvbw")]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("dQw4w9WgXcQ")
    assert resolution.resolved
    assert resolution.state is ResolveState.LIVE
    assert resolution.live_chat_id == "Cg0KC2Zvbw"
    assert resolution.title == "Video"


async def test_upcoming_video():
    def handler(request):
        return httpx.Response(200, json={
            "items": [_video("dQw4w9WgXcQ", live="upcoming")]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("dQw4w9WgXcQ")
    assert resolution.state is ResolveState.UPCOMING
    assert not resolution.resolved


async def test_ended_video():
    def handler(request):
        video = _video("dQw4w9WgXcQ")
        video["liveStreamingDetails"]["actualEndTime"] = "2026-10-15T09:00:00Z"
        return httpx.Response(200, json={"items": [video]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("dQw4w9WgXcQ")
    assert resolution.state is ResolveState.ENDED


async def test_unknown_video_id():
    def handler(request):
        return httpx.Response(200, json={"items": []}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("dQw4w9WgXcQ")
    assert resolution.state is ResolveState.NOT_FOUND


async def test_live_without_chat_id_still_live():
    # liveBroadcastContent=live but no activeLiveChatId yet: LIVE, re-resolve retries
    def handler(request):
        return httpx.Response(200, json={
            "items": [_video("dQw4w9WgXcQ", live="live")]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("dQw4w9WgXcQ")
    assert resolution.state is ResolveState.LIVE
    assert not resolution.resolved


# -- channel resolution -----------------------------------------------------------------


async def test_channel_live_video_wins():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/channels"):
            assert request.url.params["forHandle"] == "forsen"
            return httpx.Response(200, json=_channel(), request=request)
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(200, json={
                "items": [
                    {"contentDetails": {"videoId": "newestLive"}},
                    {"contentDetails": {"videoId": "olderVideo"}},
                ]}, request=request)
        if request.url.path.endswith("/videos"):
            ids = request.url.params["id"].split(",")
            return httpx.Response(200, json={"items": [
                _video(video_id, live="live" if video_id == "newestLive" else "none",
                       chat_id="Cg0KC2Zvbw" if video_id == "newestLive" else None)
                for video_id in ids
            ]}, request=request)
        raise AssertionError(request.url.path)

    resolution = await LiveChatResolver(_api(handler)).resolve("@forsen")
    assert resolution.resolved
    assert resolution.video_id == "newestLive"
    assert resolution.live_chat_id == "Cg0KC2Zvbw"
    assert sum(1 for c in calls if c.endswith("/videos")) == 1  # one batch of 2


async def test_channel_newest_offline_video():
    def handler(request):
        if request.url.path.endswith("/channels"):
            return httpx.Response(200, json=_channel(), request=request)
        if request.url.path.endswith("/playlistItems"):
            return httpx.Response(200, json={
                "items": [{"contentDetails": {"videoId": "newestVideo"}}]}, request=request)
        if request.url.path.endswith("/videos"):
            return httpx.Response(200, json={
                "items": [_video("newestVideo", live="none")]}, request=request)
        raise AssertionError(request.url.path)

    resolution = await LiveChatResolver(_api(handler)).resolve("@forsen")
    assert resolution.state is ResolveState.OFFLINE
    assert resolution.video_id == "newestVideo"


async def test_channel_videos_batched_at_50():
    video_ids = [f"{'v' * 10}{i:02d}" for i in range(120)]  # >2 batches of 50
    batch_sizes = []

    def handler(request):
        if request.url.path.endswith("/channels"):
            return httpx.Response(200, json=_channel(), request=request)
        if request.url.path.endswith("/playlistItems"):
            items = [{"contentDetails": {"videoId": vid}} for vid in video_ids[:60]]
            return httpx.Response(200, json={"items": items}, request=request)
        if request.url.path.endswith("/videos"):
            ids = request.url.params["id"].split(",")
            batch_sizes.append(len(ids))
            return httpx.Response(200, json={
                "items": [_video(vid) for vid in ids]}, request=request)
        raise AssertionError(request.url.path)

    await LiveChatResolver(_api(handler)).resolve("@forsen")
    assert batch_sizes == [BATCH, 60 - BATCH]  # 50 + 10


async def test_channel_playlist_paging_caps_at_max_pages():
    pages_seen = 0

    def handler(request):
        nonlocal pages_seen
        if request.url.path.endswith("/channels"):
            return httpx.Response(200, json=_channel(), request=request)
        if request.url.path.endswith("/playlistItems"):
            pages_seen += 1
            # every page has a nextPageToken: must stop at MAX_PLAYLIST_PAGES
            return httpx.Response(200, json={
                "items": [{"contentDetails": {"videoId": f"{'p' * 10}{pages_seen}"}}],
                "nextPageToken": f"tok{pages_seen}"}, request=request)
        if request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"items": []}, request=request)
        raise AssertionError(request.url.path)

    await LiveChatResolver(_api(handler)).resolve("@forsen")
    assert pages_seen == MAX_PLAYLIST_PAGES


async def test_uc_prefixed_handle_uses_handle_endpoint():
    # "UC…" that is NOT a 24-char channel id must still resolve as a handle.
    seen = {}

    def handler(request):
        seen["forHandle"] = request.url.params.get("forHandle")
        seen["id"] = request.url.params.get("id")
        return httpx.Response(200, json={
            "items": [{"id": "UCrealchannel",
                       "contentDetails": {"relatedPlaylists": {}}}]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("UCforsenStyle1Handle")
    assert seen["forHandle"] == "UCforsenStyle1Handle"
    assert seen["id"] is None  # channels.list?forHandle=, never ?id=
    assert resolution.state is ResolveState.OFFLINE


async def test_channel_not_found():
    def handler(request):
        return httpx.Response(404, json={
            "error": {"errors": [{"reason": "notFound"}]}}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("@nope")
    assert resolution.state is ResolveState.NOT_FOUND


# -- mine target (Stream tab) ---------------------------------------------------------


def test_parse_target_mine_sentinel():
    assert parse_target("mine:") == ("mine", "")
    assert parse_target("MINE:") == ("mine", "")
    assert parse_target(" mine: ") == ("mine", "")


def test_parse_target_bare_mine_is_still_a_handle():
    # The colon is what makes it the sentinel; "mine" alone is a real handle.
    assert parse_target("mine") == ("handle", "mine")


def _broadcast(bid, status="live", chat_id="Cg0KC2Zvbw", title="My Stream",
               channel="Me", channel_id="UCme", viewers=42):
    broadcast = {
        "id": bid,
        "snippet": {"title": title, "channelTitle": channel, "channelId": channel_id},
        "status": {"lifeCycleStatus": status},
        "statistics": {"concurrentViewers": str(viewers)},
    }
    if chat_id:
        broadcast["snippet"]["liveChatId"] = chat_id
    return broadcast


async def test_mine_offline_when_no_broadcasts():
    def handler(request):
        assert request.url.path.endswith("/liveBroadcasts")
        return httpx.Response(200, json={"items": []}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("mine:")
    assert resolution.state is ResolveState.OFFLINE


async def test_mine_live_resolves_with_statistics_viewers():
    def handler(request):
        return httpx.Response(200, json={"items": [
            _broadcast("b1", viewers=777)]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("mine:")
    assert resolution.resolved
    assert resolution.state is ResolveState.LIVE
    assert resolution.video_id == "b1"
    assert resolution.live_chat_id == "Cg0KC2Zvbw"
    assert resolution.title == "My Stream"
    assert resolution.channel_id == "UCme"
    assert resolution.viewers == 777


async def test_mine_live_without_chat_id_unresolved():
    def handler(request):
        return httpx.Response(200, json={"items": [
            _broadcast("b1", chat_id=None)]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("mine:")
    assert resolution.state is ResolveState.LIVE
    assert not resolution.resolved


async def test_mine_ready_maps_to_upcoming_with_schedule():
    def handler(request):
        broadcast = _broadcast("b1", status="ready", chat_id=None)
        broadcast["snippet"]["scheduledStartTime"] = "2026-10-17T18:00:00Z"
        return httpx.Response(200, json={"items": [broadcast]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("mine:")
    assert resolution.state is ResolveState.UPCOMING
    assert resolution.live_chat_id is None
    assert resolution.scheduled_start == dt.datetime(2026, 10, 17, 18, 0, tzinfo=dt.UTC)


async def test_mine_prefers_live_over_created():
    def handler(request):
        return httpx.Response(200, json={"items": [
            _broadcast("b-created", status="created", chat_id=None),
            _broadcast("b-live", status="live", chat_id="Cg0KC2Zvbw"),
        ]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("mine:")
    assert resolution.resolved
    assert resolution.video_id == "b-live"


async def test_mine_complete_maps_to_offline():
    def handler(request):
        return httpx.Response(200, json={"items": [
            _broadcast("b1", status="complete", chat_id=None)]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("mine:")
    assert resolution.state is ResolveState.OFFLINE


async def test_live_video_carries_viewers_and_started_at():
    def handler(request):
        video = _video("dQw4w9WgXcQ", live="live", chat_id="Cg0KC2Zvbw")
        video["liveStreamingDetails"]["concurrentViewers"] = "1234"
        video["liveStreamingDetails"]["actualStartTime"] = "2026-10-16T10:00:00Z"
        return httpx.Response(200, json={"items": [video]}, request=request)

    resolution = await LiveChatResolver(_api(handler)).resolve("dQw4w9WgXcQ")
    assert resolution.viewers == 1234
    assert resolution.started_at == dt.datetime(2026, 10, 16, 10, 0, tzinfo=dt.UTC)
