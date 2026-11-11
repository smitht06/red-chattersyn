"""Fills a user card with account details as each lookup answers (Twitch Helix
or YouTube channels.list). Moved out of main_window.py; MainWindow keeps thin
delegates."""

from __future__ import annotations

import asyncio
import logging

from ..models.message import Platform
from ..services.twitch.auth import FOLLOWERS_SCOPE
from .usercard import UsercardPopup, UsercardTarget

log = logging.getLogger("chattersyn.ui.main_window")  # same logger as before the move


async def enrich_usercard(win, popup: UsercardPopup, target: UsercardTarget,
                           channel: str, self_is_mod: bool) -> None:
    """Helix/YouTube field expansion; the popup may have closed meanwhile.
    `channel` is the Twitch channel of the tab the card came from."""
    try:
        if target.platform is Platform.TWITCH:
            await win._enrich_twitch_usercard(popup, target, channel, self_is_mod)
        else:
            await win._enrich_youtube_usercard(popup, target)
    except Exception as exc:  # enrichment is best-effort
        log.debug("usercard enrichment failed: %s", exc)


async def enrich_twitch_usercard(win, popup: UsercardPopup, target: UsercardTarget,
                                  channel: str, self_is_mod: bool) -> None:
    """Each detail is its own lookup and fills its own row as it answers,
    so one failing lookup never hides the others. The follow date is
    about `channel` and only its broadcaster and mods may ask for it."""
    if win._twitch is None or win._twitch.helix is None:
        return  # anonymous read: Author fields only
    helix = win._twitch.helix
    user_id = target.user_id
    try:
        user = await helix.user_by_login(target.login)
    except Exception as exc:  # keep going with the id from the chat row
        log.debug("usercard user lookup failed: %s", exc)
    else:
        if user is None:
            return  # no such Twitch user (renamed or deleted)
        if win._usercard_popup is not popup:
            return
        popup.set_details(created_at=user.get("created_at", ""),
                          avatar_url=user.get("profile_image_url") or None)
        user_id = user.get("id") or user_id
    if not user_id or win._usercard_popup is not popup:
        return
    await asyncio.gather(
        win._usercard_follower_total(popup, user_id),
        win._usercard_follow_date(popup, user_id, channel, self_is_mod),
    )


async def usercard_follower_total(win, popup: UsercardPopup, user_id: str) -> None:
    try:
        followers = await win._twitch.helix.channel_followers(user_id)
    except Exception as exc:
        log.debug("usercard follower count failed: %s", exc)
        return
    if followers is not None and win._usercard_popup is popup:
        popup.set_details(followers=followers)


async def usercard_follow_date(win, popup: UsercardPopup, user_id: str,
                                channel: str, self_is_mod: bool) -> None:
    tokens = win._twitch.tokens
    if not (self_is_mod and channel and tokens is not None
            and FOLLOWERS_SCOPE in tokens.scopes):
        return
    try:
        broadcaster = await win._twitch.helix.user_by_login(channel)
        if broadcaster is None or not broadcaster.get("id") or broadcaster["id"] == user_id:
            return  # unknown channel, or the broadcaster's own card
        followed_at = await win._twitch.helix.channel_follower_since(
            broadcaster["id"], user_id)
    except Exception as exc:
        log.debug("usercard follow lookup failed: %s", exc)
        return
    if followed_at and win._usercard_popup is popup:
        popup.set_details(followage=followed_at, followage_channel=channel)


async def enrich_youtube_usercard(win, popup: UsercardPopup,
                                   target: UsercardTarget) -> None:
    if win._youtube is None or not target.user_id:
        return
    try:
        data = await win._youtube.api.channel_by_id(target.user_id)
    except Exception:  # no OAuth/API key: skip silently
        return
    if data is None or win._usercard_popup is not popup:
        return
    items = data.get("items", [])
    if not items:
        return
    item = items[0]
    subscribers = str(item.get("statistics", {}).get("subscriberCount") or "")
    popup.set_details(
        created_at=item.get("snippet", {}).get("publishedAt", ""),
        followers=int(subscribers) if subscribers.isdigit() else None,
    )
