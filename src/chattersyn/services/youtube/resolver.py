"""liveChatId resolution: video id/URL -> videos.list; channel handle ->
uploads playlist scan. All paths verified against the Data API docs (2026-10).

Key facts: activeLiveChatId exists only while the broadcast is LIVE (removed
at end — there is no replay chat API); snippet.liveBroadcastContent is
live/upcoming/none; playlistItems.list has no live filter, so the newest
upload is picked by snippet.publishedAt. videos.list batches: 50 ids max.
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
import re

from .api import YouTubeApi, YoutubeApiError

log = logging.getLogger(__name__)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
VIDEO_URL_RES = (
    re.compile(r"youtube\.com/watch\?(?:.*&)?v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/live/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})"),
)
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
HANDLE_RE = re.compile(r"^@?[A-Za-z0-9_.\-]{3,30}$")
BATCH = 50
MAX_PLAYLIST_PAGES = 10  # newest uploads first in practice; cap the scan

# Sentinel target for "the signed-in user's own live stream". The colon makes
# it untypable as a real handle/id, so parse_target can never mis-hit it.
MINE_TARGET = "mine:"


class ResolveState(enum.Enum):
    LIVE = "live"
    UPCOMING = "upcoming"
    OFFLINE = "offline"  # channel/stream has no active live chat right now
    ENDED = "ended"
    NOT_FOUND = "not_found"


class Resolution:
    """Outcome of one resolve attempt."""

    def __init__(
        self,
        state: ResolveState,
        video_id: str | None = None,
        live_chat_id: str | None = None,
        title: str = "",
        channel_title: str = "",
        channel_id: str | None = None,
        scheduled_start: dt.datetime | None = None,
        viewers: int = 0,
        started_at: dt.datetime | None = None,
        scheduled_starts: list[dt.datetime] | None = None,
    ):
        self.state = state
        self.video_id = video_id
        self.live_chat_id = live_chat_id
        self.title = title
        self.channel_title = channel_title
        self.channel_id = channel_id
        self.scheduled_start = scheduled_start
        self.viewers = viewers  # liveStreamingDetails.concurrentViewers
        self.started_at = started_at  # liveStreamingDetails.actualStartTime
        # Start times of every upcoming video seen (stale ones included; the
        # scheduler filters them).
        self.scheduled_starts: list[dt.datetime] = list(scheduled_starts or [])

    @property
    def resolved(self) -> bool:
        return self.state is ResolveState.LIVE and self.live_chat_id is not None


def parse_target(target: str) -> tuple[str, str] | None:
    """Normalize a pasted target -> (kind, id). kind: video|handle|channel|mine.
    None = unrecognized."""
    text = target.strip()
    if text.lower() == MINE_TARGET:
        return "mine", ""
    for pattern in VIDEO_URL_RES:
        match = pattern.search(text)
        if match:
            return "video", match.group(1)
    if VIDEO_ID_RE.fullmatch(text):
        return "video", text
    if CHANNEL_ID_RE.fullmatch(text):
        return "channel", text
    if HANDLE_RE.fullmatch(text.lstrip("@")):
        return "handle", text.lstrip("@")
    return None


class _ChannelCache:
    """What a channel target remembers so re-checks read one page only."""

    def __init__(self, channel_id: str, uploads: str, page_ids: list[str],
                 videos: list[dict]):
        self.channel_id = channel_id
        self.uploads = uploads
        self.page_ids = page_ids  # ids on the newest page at the last check
        self.videos = videos  # videos from the last videos_batch (newest first)


class LiveChatResolver:
    def __init__(self, api: YouTubeApi):
        self._api = api
        self._cache: dict[tuple[str, str], _ChannelCache] = {}

    def forget(self, target: str) -> None:
        """Drop what is remembered for a channel target."""
        parsed = parse_target(target)
        if parsed is not None:
            self._cache.pop(parsed, None)

    async def resolve(self, target: str, *, deep: bool = False) -> Resolution:
        """Resolve a video URL/id, a channel handle/id, or MINE_TARGET (the
        authorized account's own live broadcast) to a live chat. A channel
        resolve is a full scan when deep or not yet cached; otherwise it reads
        only the newest uploads page."""
        parsed = parse_target(target)
        if parsed is None:
            return Resolution(ResolveState.NOT_FOUND)
        kind, value = parsed
        if kind == "video":
            return await self._from_video(value)
        if kind == "mine":
            return await self._from_mine()
        cached = None if deep else self._cache.get(parsed)
        if cached is not None:
            return await self._recheck_channel(parsed, cached)
        return await self._from_channel(value, kind)

    async def _from_mine(self) -> Resolution:
        """liveBroadcasts.list mine=true: the authorized account's own active
        broadcast (1 unit). Empty = not live — the waiting state."""
        items = await self._api.live_broadcasts_mine()
        if not items:
            return Resolution(ResolveState.OFFLINE)
        items = sorted(items, key=lambda i: self._MINE_PRIORITY.get(
            (i.get("status") or {}).get("lifeCycleStatus", ""), 99))
        item = items[0]
        snippet = item.get("snippet") or {}
        status = (item.get("status") or {}).get("lifeCycleStatus", "")
        if status == "live":
            state = ResolveState.LIVE
        elif status in ("liveStarting", "testStarting", "testing", "ready", "created"):
            state = ResolveState.UPCOMING  # chat not joinable before the stream starts
        else:  # complete / revoked / unknown — nothing watchable right now
            return Resolution(ResolveState.OFFLINE)
        return Resolution(
            state=state,
            video_id=item.get("id"),
            live_chat_id=snippet.get("liveChatId"),
            title=snippet.get("title", ""),
            channel_title=snippet.get("channelTitle", ""),
            channel_id=snippet.get("channelId"),
            scheduled_start=_parse_dt(snippet.get("scheduledStartTime")),
            scheduled_starts=_starts([snippet.get("scheduledStartTime")]),
            viewers=int(((item.get("statistics") or {}).get("concurrentViewers")) or 0),
            started_at=_parse_dt(snippet.get("actualStartTime")),
        )

    _MINE_PRIORITY = {"live": 0, "liveStarting": 1, "testStarting": 2,
                      "testing": 3, "ready": 4, "created": 5}

    async def _from_video(self, video_id: str) -> Resolution:
        data = await self._api.videos(video_id)
        if data is None or not data.get("items"):
            return Resolution(ResolveState.NOT_FOUND, video_id=video_id)
        return self._video_resolution(data["items"][0])

    async def _from_channel(self, channel: str, kind: str) -> Resolution:
        if kind == "channel":
            data = await self._api.channel_by_id(channel)
        else:
            data = await self._api.channel_by_handle(channel)
        if data is None or not data.get("items"):
            return Resolution(ResolveState.NOT_FOUND)
        item = data["items"][0]
        channel_id = item["id"]
        uploads = (
            item.get("contentDetails", {})
            .get("relatedPlaylists", {})
            .get("uploads", "")
        )
        if not uploads:
            return Resolution(ResolveState.OFFLINE, channel_id=channel_id)
        scan = await self._newest_videos(uploads)
        if scan is None:
            # No public uploads: the channel exists but is offline. Nothing is
            # cached, so each check is this lookup plus the 404 (2 units).
            self._cache.pop((kind, channel), None)
            return Resolution(ResolveState.OFFLINE, channel_id=channel_id)
        videos, page_ids = scan
        self._cache[(kind, channel)] = _ChannelCache(channel_id, uploads, page_ids, videos)
        return self._channel_resolution(channel_id, videos)

    def _channel_resolution(self, channel_id: str, videos: list[dict]) -> Resolution:
        starts = self._upcoming_starts(videos)
        for video in videos:
            if self._live_state(video) == "live":
                res = self._video_resolution(video)
                res.scheduled_starts = starts
                return res
        if not videos:
            return Resolution(ResolveState.OFFLINE, channel_id=channel_id)
        res = self._video_resolution(videos[0])  # newest: upcoming/ended/offline
        res.scheduled_starts = starts
        return res

    def _upcoming_starts(self, videos: list[dict]) -> list[dt.datetime]:
        return _starts(
            video.get("liveStreamingDetails", {}).get("scheduledStartTime")
            for video in videos if self._live_state(video) == "upcoming")

    async def _recheck_channel(
        self, key: tuple[str, str], cache: _ChannelCache
    ) -> Resolution:
        """One playlistItems page (1 unit), plus one videos.list unless the
        page is unchanged and nothing on it is upcoming or live."""
        try:
            page = await self._api.playlist_items(cache.uploads)
        except YoutubeApiError as exc:
            if exc.http_status == 404:
                self._cache.pop(key, None)
                return Resolution(ResolveState.NOT_FOUND)
            raise
        ids = _page_ids(page)
        watching = [v for v in cache.videos if self._live_state(v) in ("upcoming", "live")]
        if ids == cache.page_ids and not watching:
            return self._channel_resolution(cache.channel_id, cache.videos)
        # Also refresh a watched video that has slipped off the newest page
        # (a long-running stream found by the deep scan).
        extra = [v["id"] for v in watching if v.get("id") not in ids]
        # One videos.list call only (<= 50 ids): watched off-page ids first,
        # the newest page fills the remaining slots.
        wanted = [*extra[:BATCH], *ids][:BATCH]
        videos = await self._api.videos_batch(wanted)
        videos.sort(key=lambda v: v.get("snippet", {}).get("publishedAt", ""), reverse=True)
        cache.page_ids = ids
        cache.videos = videos
        return self._channel_resolution(cache.channel_id, videos)

    async def _newest_videos(
        self, uploads_playlist: str
    ) -> tuple[list[dict], list[str]] | None:
        """Deep scan -> (videos newest first, ids on the newest page).
        None when the first page is a 404: YouTube's answer for the uploads
        playlist of a channel with no public uploads."""
        videos: list[dict] = []
        first_page: list[str] | None = None
        page_token: str | None = None
        for _ in range(MAX_PLAYLIST_PAGES):
            try:
                page = await self._api.playlist_items(uploads_playlist, page_token=page_token)
            except YoutubeApiError as exc:
                if exc.http_status == 404 and first_page is None:
                    return None
                raise
            ids = _page_ids(page)
            if first_page is None:
                first_page = ids
            for start in range(0, len(ids), BATCH):
                videos.extend(await self._api.videos_batch(ids[start:start + BATCH]))
            page_token = page.get("nextPageToken")
            if not page_token or any(self._live_state(v) == "live" for v in videos):
                break
        videos.sort(key=lambda v: v.get("snippet", {}).get("publishedAt", ""), reverse=True)
        return videos, first_page or []

    @staticmethod
    def _live_state(video: dict) -> str:
        return video.get("snippet", {}).get("liveBroadcastContent", "none")

    def _video_resolution(self, video: dict) -> Resolution:
        details = video.get("liveStreamingDetails", {})
        live_chat_id = details.get("activeLiveChatId")
        live = self._live_state(video)
        snippet = video.get("snippet", {})
        if live_chat_id:
            state = ResolveState.LIVE
        elif live == "upcoming":
            state = ResolveState.UPCOMING
        elif live == "live":
            state = ResolveState.LIVE  # chat id can lag a moment; re-resolve retries
        elif details.get("actualEndTime"):
            state = ResolveState.ENDED
        else:
            state = ResolveState.OFFLINE
        return Resolution(
            state=state,
            video_id=video.get("id"),
            live_chat_id=live_chat_id,
            title=snippet.get("title", ""),
            channel_title=snippet.get("channelTitle", ""),
            channel_id=snippet.get("channelId"),
            scheduled_start=_parse_dt(details.get("scheduledStartTime")),
            scheduled_starts=_starts([details.get("scheduledStartTime")]),
            viewers=int(details.get("concurrentViewers") or 0),
            started_at=_parse_dt(details.get("actualStartTime")),
        )


def _page_ids(page: dict) -> list[str]:
    return [
        item["contentDetails"]["videoId"]
        for item in page.get("items", [])
        if "videoId" in item.get("contentDetails", {})
    ]


def _starts(values) -> list[dt.datetime]:
    return [d for d in (_parse_dt(v) for v in values) if d is not None]


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
