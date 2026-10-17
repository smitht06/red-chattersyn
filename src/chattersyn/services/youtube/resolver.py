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

from .api import YouTubeApi

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

    @property
    def resolved(self) -> bool:
        return self.state is ResolveState.LIVE and self.live_chat_id is not None


def parse_target(target: str) -> tuple[str, str] | None:
    """Normalize a pasted target -> (kind, id). kind: video|handle|channel.
    None = unrecognized."""
    text = target.strip()
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


class LiveChatResolver:
    def __init__(self, api: YouTubeApi):
        self._api = api

    async def resolve(self, target: str) -> Resolution:
        """Resolve a video URL/id or a channel handle/id to a live chat."""
        parsed = parse_target(target)
        if parsed is None:
            return Resolution(ResolveState.NOT_FOUND)
        kind, value = parsed
        if kind == "video":
            return await self._from_video(value)
        return await self._from_channel(value, kind)

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
        videos = await self._newest_videos(uploads)
        for video in videos:
            if self._live_state(video) == "live":
                return self._video_resolution(video)
        if not videos:
            return Resolution(ResolveState.OFFLINE, channel_id=channel_id)
        return self._video_resolution(videos[0])  # newest: upcoming/ended/offline

    async def _newest_videos(self, uploads_playlist: str) -> list[dict]:
        videos: list[dict] = []
        page_token: str | None = None
        for _ in range(MAX_PLAYLIST_PAGES):
            page = await self._api.playlist_items(uploads_playlist, page_token=page_token)
            ids = [
                item["contentDetails"]["videoId"]
                for item in page.get("items", [])
                if "videoId" in item.get("contentDetails", {})
            ]
            for start in range(0, len(ids), BATCH):
                videos.extend(await self._api.videos_batch(ids[start:start + BATCH]))
            page_token = page.get("nextPageToken")
            if not page_token or any(self._live_state(v) == "live" for v in videos):
                break
        videos.sort(key=lambda v: v.get("snippet", {}).get("publishedAt", ""), reverse=True)
        return videos

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
            viewers=int(details.get("concurrentViewers") or 0),
            started_at=_parse_dt(details.get("actualStartTime")),
        )


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
