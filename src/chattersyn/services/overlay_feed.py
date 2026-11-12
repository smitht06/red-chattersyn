"""Chat rows for the OBS browser source: `message_to_event` turns one Message
into the JSON event the overlay page draws (pure, no Qt)."""

from __future__ import annotations

import time
from typing import Any

from ..models.message import ImageRun, Message, MessageFlags, Platform, TextRun

# Same bar colours as the chat view (ui/message_delegate.py).
SUB_BAR = "#00b3ff"
ANNOUNCE_BARS = {"blue": "#1e90ff", "green": "#2ba640", "orange": "#ff8c1a", "purple": "#a970ff"}
SUPERCHAT_BAR = "#f6c344"
MEMBERSHIP_BAR = "#2ba640"
CHEER_BAR = "#9146ff"


def _http(url: str | None) -> str | None:
    """Only http(s) image addresses reach the page."""
    if url and (url.startswith("https://") or url.startswith("http://")):
        return url
    return None


def _kind_and_bar(msg: Message) -> tuple[str, str | None]:
    flags = msg.flags
    if flags & MessageFlags.SUBSCRIPTION:
        return "sub", SUB_BAR
    if flags & MessageFlags.ANNOUNCEMENT:
        return "announcement", ANNOUNCE_BARS.get(msg.extra.get("color", ""), ANNOUNCE_BARS["purple"])
    if flags & MessageFlags.SUPERCHAT:
        return "superchat", SUPERCHAT_BAR
    if flags & MessageFlags.MEMBERSHIP:
        return "membership", MEMBERSHIP_BAR
    if flags & MessageFlags.CHEER:
        return "cheer", CHEER_BAR
    if msg.extra.get("msg_id") == "raid":
        return "raid", None
    if msg.is_system:
        return "event", None
    if flags & MessageFlags.ACTION:
        return "action", None
    return "normal", None


def _from_chat(msg: Message) -> bool:
    """Rows viewers made: chat messages, and Twitch chat events (subs, raids,
    announcements). Not the app's notes, Twitch NOTICE rows (errors, command
    feedback) or YouTube's moderation/status notes. Super Chats and
    memberships aren't system rows, so they pass."""
    if msg.platform not in (Platform.TWITCH, Platform.YOUTUBE):
        return False
    if msg.is_system:
        return msg.platform is Platform.TWITCH and bool(msg.extra.get("usernotice"))
    return True


def message_to_event(msg: Message, *, now: float | None = None) -> dict[str, Any] | None:
    """The overlay event for `msg`, or None when it isn't shown on stream (app
    notes, deleted or greyed-out rows)."""
    if msg.flags & (MessageFlags.DELETED | MessageFlags.DISABLED):
        return None
    if not _from_chat(msg):
        return None
    parts: list[dict[str, str]] = []
    for run in msg.runs:
        if isinstance(run, ImageRun):
            url = _http(run.emote.url_for_scale("2x"))
            if url:
                parts.append({"t": "emote", "url": url, "name": run.emote.name})
            else:
                parts.append({"t": "text", "v": run.emote.name})
        elif isinstance(run, TextRun):
            parts.append({"t": "text", "v": run.text})
    if not parts and msg.plain:
        parts.append({"t": "text", "v": msg.plain})
    author = msg.author
    badges = []
    if author is not None:
        for badge in author.badges:
            url = _http(badge.url_for(2.0))
            if url:
                badges.append({"url": url, "title": badge.title or badge.set_id})
    kind, bar = _kind_and_bar(msg)
    return {
        "id": msg.id,
        "platform": msg.platform.value,
        "name": author.name if author is not None else "",
        "color": author.color if author is not None else None,
        "badges": badges,
        "parts": parts,
        # Local time for both platforms (YouTube times are UTC-aware).
        "ts": msg.ts.astimezone().strftime("%H:%M") if hasattr(msg.ts, "astimezone") else "",
        "arrived": time.time() if now is None else now,
        "kind": kind,
        "bar": bar,
        "amount": _superchat_amount(msg) if kind == "superchat" else "",
    }


def _superchat_amount(msg: Message) -> str:
    details = msg.extra.get("superchat")
    if isinstance(details, dict):
        return str(details.get("amountDisplayString") or "")
    return ""
