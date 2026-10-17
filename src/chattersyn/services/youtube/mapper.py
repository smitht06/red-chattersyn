"""YouTube live chat events -> Message.

Both sources map through the same REST-shaped dict shape: the gRPC reader
normalizes protobuf LiveChatMessage objects into it, the REST fallback
produces it from response JSON directly, and map_item() builds the render
model. Type strings are the REST snakeCase names (textMessageEvent, ...).

Author badges: the API has no badge image set, so owner/moderator/verified/
member render as url-less Badge objects the delegate draws as text chips.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from ...models.message import Author, Badge, Message, MessageFlags, Platform, TextRun
from . import stream_list_pb2 as pb

log = logging.getLogger(__name__)

# pb2 enum value -> REST type string
PB2_TO_REST_TYPE = {
    pb.LiveChatMessageSnippet.TypeWrapper.TEXT_MESSAGE_EVENT: "textMessageEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.TOMBSTONE: "tombstone",
    pb.LiveChatMessageSnippet.TypeWrapper.FAN_FUNDING_EVENT: "fanFundingEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.CHAT_ENDED_EVENT: "chatEndedEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.SPONSOR_ONLY_MODE_STARTED_EVENT: "sponsorOnlyModeStartedEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.SPONSOR_ONLY_MODE_ENDED_EVENT: "sponsorOnlyModeEndedEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.NEW_SPONSOR_EVENT: "newSponsorEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.MEMBER_MILESTONE_CHAT_EVENT: "memberMilestoneChatEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.MEMBERSHIP_GIFTING_EVENT: "membershipGiftingEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.GIFT_MEMBERSHIP_RECEIVED_EVENT: "giftMembershipReceivedEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.USER_BANNED_EVENT: "userBannedEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.SUPER_CHAT_EVENT: "superChatEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.SUPER_STICKER_EVENT: "superStickerEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.POLL_EVENT: "pollEvent",
    pb.LiveChatMessageSnippet.TypeWrapper.GIFT_EVENT: "giftEvent",
}


def pb2_to_item(item) -> dict:
    """Normalize a protobuf LiveChatMessage into the REST-shaped item dict."""
    sn = item.snippet
    snippet: dict[str, Any] = {}
    if sn.HasField("type"):
        snippet["type"] = PB2_TO_REST_TYPE.get(sn.type, "")
    if sn.HasField("live_chat_id"):
        snippet["liveChatId"] = sn.live_chat_id
    if sn.HasField("author_channel_id"):
        snippet["authorChannelId"] = sn.author_channel_id
    if sn.HasField("published_at"):
        snippet["publishedAt"] = sn.published_at
    if sn.HasField("has_display_content"):
        snippet["hasDisplayContent"] = sn.has_display_content
    if sn.HasField("display_message"):
        snippet["displayMessage"] = sn.display_message

    def details(obj) -> dict:
        # REST field names are camelCase; proto names are snake_case.
        # Generic conversion keeps map_item()'s camelCase reads working.
        def camel(name: str) -> str:
            head, *rest = name.split("_")
            return head + "".join(part.capitalize() for part in rest)

        return {camel(f.name): getattr(obj, f.name)
                for f in obj.DESCRIPTOR.fields
                if obj.HasField(f.name) and f.name not in {"type"}}

    if sn.HasField("text_message_details"):
        snippet["textMessageDetails"] = {"messageText": sn.text_message_details.message_text}
    if sn.HasField("user_banned_details"):
        banned = sn.user_banned_details
        snippet["userBannedDetails"] = {
            "bannedUserDetails": details(banned.banned_user_details),
            "banType": "permanent"
            if banned.ban_type == pb.LiveChatUserBannedMessageDetails.BanTypeWrapper.PERMANENT
            else "temporary",
        }
        if banned.HasField("ban_duration_seconds"):
            snippet["userBannedDetails"]["banDurationSeconds"] = banned.ban_duration_seconds
    if sn.HasField("super_chat_details"):
        snippet["superChatDetails"] = details(sn.super_chat_details)
    if sn.HasField("super_sticker_details"):
        sticker = details(sn.super_sticker_details)
        if sn.super_sticker_details.HasField("super_sticker_metadata"):
            sticker["superStickerMetadata"] = details(
                sn.super_sticker_details.super_sticker_metadata)
        snippet["superStickerDetails"] = sticker
    if sn.HasField("new_sponsor_details"):
        snippet["newSponsorDetails"] = details(sn.new_sponsor_details)
    if sn.HasField("member_milestone_chat_details"):
        snippet["memberMilestoneChatDetails"] = details(sn.member_milestone_chat_details)
    if sn.HasField("membership_gifting_details"):
        snippet["membershipGiftingDetails"] = details(sn.membership_gifting_details)
    if sn.HasField("gift_membership_received_details"):
        snippet["giftMembershipReceivedDetails"] = details(sn.gift_membership_received_details)

    result = {"id": item.id, "snippet": snippet}
    if item.HasField("author_details"):
        ad = item.author_details
        author: dict[str, Any] = {}
        for name, key in (
            ("channel_id", "channelId"),
            ("display_name", "displayName"),
            ("profile_image_url", "profileImageUrl"),
            ("is_verified", "isVerified"),
            ("is_chat_owner", "isChatOwner"),
            ("is_chat_sponsor", "isChatSponsor"),
            ("is_chat_moderator", "isChatModerator"),
        ):
            if ad.HasField(name):
                author[key] = getattr(ad, name)
        result["authorDetails"] = author
    return result


def _author(author_details: dict | None, fallback_name: str = "") -> Author | None:
    if not author_details:
        return None
    badges = []
    if author_details.get("isChatOwner"):
        badges.append(Badge("yt", "owner", "Owner"))
    if author_details.get("isChatModerator"):
        badges.append(Badge("yt", "moderator", "Moderator"))
    if author_details.get("isVerified"):
        badges.append(Badge("yt", "verified", "Verified"))
    if author_details.get("isChatSponsor"):
        badges.append(Badge("yt", "member", "Member"))
    return Author(
        name=author_details.get("displayName") or fallback_name,
        login=(author_details.get("channelId") or "").lower(),
        user_id=author_details.get("channelId", ""),
        badges=badges,
        avatar_url=author_details.get("profileImageUrl"),
    )


def map_item(item: dict) -> Message:
    """Map a REST-shaped live chat item to a render Message."""
    snippet = item.get("snippet", {}) or {}
    event_type = snippet.get("type", "")
    channel_id = snippet.get("liveChatId", "")
    ts = _parse_dt(snippet.get("publishedAt")) or dt.datetime.now()
    display = snippet.get("displayMessage", "")
    author = _author(item.get("authorDetails"))
    msg_id = item.get("id", "")
    extra = {"youtube_type": event_type}

    def build(flags: MessageFlags, text: str, **kwargs) -> Message:
        return Message(
            id=msg_id,
            platform=Platform.YOUTUBE,
            channel_id=channel_id,
            ts=kwargs.pop("ts", ts),
            author=kwargs.pop("author", author),
            runs=kwargs.pop("runs", [TextRun(text)]),
            plain=text,
            flags=flags,
            extra=kwargs.pop("extra", extra),
            **kwargs,
        )

    if event_type == "textMessageEvent":
        text = (snippet.get("textMessageDetails") or {}).get("messageText", display)
        return build(MessageFlags.NONE, text)

    if event_type == "superChatEvent":
        details = snippet.get("superChatDetails") or {}
        text = details.get("userComment", "") or details.get("amountDisplayString", display)
        return build(MessageFlags.SUPERCHAT, text, extra={**extra, "superchat": details})

    if event_type == "superStickerEvent":
        details = snippet.get("superStickerDetails") or {}
        metadata = details.get("superStickerMetadata") or {}
        text = details.get("amountDisplayString", display)
        return build(MessageFlags.SUPERCHAT, text,
                     extra={**extra, "superchat": details,
                            "sticker_alt": metadata.get("altText", "")})

    if event_type == "fanFundingEvent":
        details = snippet.get("fanFundingDetails") or {}
        text = details.get("userComment", "") or details.get("amountDisplayString", display)
        return build(MessageFlags.SUPERCHAT, text, extra={**extra, "superchat": details})

    if event_type in {
        "newSponsorEvent", "memberMilestoneChatEvent",
        "membershipGiftingEvent", "giftMembershipReceivedEvent",
    }:
        return build(MessageFlags.MEMBERSHIP, display)

    if event_type == "tombstone":
        # Deleted message: the API provides no author or text.
        return build(MessageFlags.SYSTEM | MessageFlags.CENTERED,
                     "A message was deleted", author=None)

    if event_type == "chatEndedEvent":
        return build(MessageFlags.SYSTEM | MessageFlags.CENTERED,
                     "Live chat has ended", author=None)

    if event_type == "sponsorOnlyModeStartedEvent":
        return build(MessageFlags.SYSTEM | MessageFlags.CENTERED,
                     "Chat is now members-only", author=None)
    if event_type == "sponsorOnlyModeEndedEvent":
        return build(MessageFlags.SYSTEM | MessageFlags.CENTERED,
                     "Chat is no longer members-only", author=None)

    if event_type == "userBannedEvent":
        details = snippet.get("userBannedDetails") or {}
        name = (details.get("bannedUserDetails") or {}).get("displayName", "A user")
        return build(MessageFlags.SYSTEM | MessageFlags.CENTERED,
                     f"{name} was removed from chat by a moderator", author=None)

    # pollEvent / giftEvent / unknown: display text as a system row
    return build(MessageFlags.SYSTEM | MessageFlags.CENTERED, display, author=None)


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
