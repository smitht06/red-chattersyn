"""YouTube event mapping: REST-shaped items -> render Messages, plus the
protobuf -> REST-shaped normalization used by the gRPC reader."""

import datetime as dt

import pytest

from chattersyn.models.message import MessageFlags, Platform
from chattersyn.services.youtube import stream_list_pb2 as pb
from chattersyn.services.youtube.mapper import PB2_TO_REST_TYPE, map_item, pb2_to_item


def _item(**kwargs):
    base = {
        "id": "msg-1",
        "snippet": {
            "type": "textMessageEvent",
            "liveChatId": "Cg0KC2Zvbw",
            "publishedAt": "2026-10-15T10:00:00Z",
            "displayMessage": "hello",
            "textMessageDetails": {"messageText": "hello"},
        },
        "authorDetails": {
            "channelId": "UCaaa",
            "displayName": "Alice",
            "profileImageUrl": "http://img/a.png",
        },
    }
    base.update(kwargs)
    return base


def _pb_item(msg_id="pb-1", text="hello"):
    snippet = pb.LiveChatMessageSnippet(
        type=pb.LiveChatMessageSnippet.TypeWrapper.TEXT_MESSAGE_EVENT,
        live_chat_id="Cg0KC2Zvbw",
        published_at="2026-10-15T10:00:00Z",
        has_display_content=True,
        display_message=text,
        text_message_details=pb.LiveChatTextMessageDetails(message_text=text),
    )
    item = pb.LiveChatMessage(id=msg_id, snippet=snippet)
    item.author_details.display_name = "Alice"
    item.author_details.channel_id = "UCaaa"
    item.author_details.profile_image_url = "http://img/a.png"
    return item


# -- map_item: event types -----------------------------------------------------------


def test_text_message():
    msg = map_item(_item())
    assert msg.platform is Platform.YOUTUBE
    assert msg.plain == "hello"
    assert msg.author.name == "Alice"
    assert msg.flags == MessageFlags.NONE
    assert msg.extra["youtube_type"] == "textMessageEvent"
    assert msg.channel_id == "Cg0KC2Zvbw"
    assert msg.ts == dt.datetime(2026, 10, 15, 10, 0, tzinfo=dt.UTC)


def test_superchat_uses_comment():
    item = _item(
        snippet={"type": "superChatEvent", "liveChatId": "Cg0KC2Zvbw",
                 "displayMessage": "$5",
                 "superChatDetails": {"userComment": "great stream",
                                      "amountDisplayString": "$5.00"}})
    msg = map_item(item)
    assert msg.flags & MessageFlags.SUPERCHAT
    assert msg.plain == "great stream"
    assert msg.extra["superchat"]["amountDisplayString"] == "$5.00"


def test_superchat_falls_back_to_amount():
    item = _item(
        snippet={"type": "superChatEvent", "liveChatId": "Cg0KC2Zvbw",
                 "displayMessage": "$5",
                 "superChatDetails": {"amountDisplayString": "$5.00"}})
    assert map_item(item).plain == "$5.00"


def test_supersticker_carries_alt_text():
    item = _item(
        snippet={"type": "superStickerEvent", "liveChatId": "Cg0KC2Zvbw",
                 "displayMessage": "$2",
                 "superStickerDetails": {
                     "amountDisplayString": "$2.00",
                     "superStickerMetadata": {"altText": "Party popper"}}})
    msg = map_item(item)
    assert msg.flags & MessageFlags.SUPERCHAT
    assert msg.extra["sticker_alt"] == "Party popper"


def test_fan_funding_is_superchat():
    item = _item(
        snippet={"type": "fanFundingEvent", "liveChatId": "Cg0KC2Zvbw",
                 "displayMessage": "$1.00",
                 "fanFundingDetails": {"amountDisplayString": "$1.00"}})
    assert map_item(item).flags & MessageFlags.SUPERCHAT


@pytest.mark.parametrize("event_type", [
    "newSponsorEvent", "memberMilestoneChatEvent",
    "membershipGiftingEvent", "giftMembershipReceivedEvent",
])
def test_membership_events(event_type):
    item = _item(snippet={"type": event_type, "liveChatId": "Cg0KC2Zvbw",
                          "displayMessage": "Alice joined!"})
    msg = map_item(item)
    assert msg.flags & MessageFlags.MEMBERSHIP
    assert msg.plain == "Alice joined!"


@pytest.mark.parametrize("event_type,text", [
    ("tombstone", "A message was deleted"),
    ("chatEndedEvent", "Live chat has ended"),
    ("sponsorOnlyModeStartedEvent", "Chat is now members-only"),
    ("sponsorOnlyModeEndedEvent", "Chat is no longer members-only"),
])
def test_system_rows(event_type, text):
    msg = map_item(_item(snippet={"type": event_type, "liveChatId": "Cg0KC2Zvbw"}))
    assert msg.flags & MessageFlags.SYSTEM
    assert msg.flags & MessageFlags.CENTERED
    assert msg.plain == text
    assert msg.author is None


def test_user_banned_temporary():
    item = _item(
        snippet={"type": "userBannedEvent", "liveChatId": "Cg0KC2Zvbw",
                 "displayMessage": "x",
                 "userBannedDetails": {
                     "bannedUserDetails": {"displayName": "Troll"},
                     "banType": "temporary", "banDurationSeconds": 300}})
    msg = map_item(item)
    assert msg.plain == "Troll was removed from chat by a moderator"
    assert msg.flags & MessageFlags.SYSTEM


def test_unknown_type_renders_display_message():
    item = _item(snippet={"type": "pollEvent", "liveChatId": "Cg0KC2Zvbw",
                          "displayMessage": "Poll: yes or no?"})
    msg = map_item(item)
    assert msg.plain == "Poll: yes or no?"
    assert msg.flags & MessageFlags.SYSTEM


def test_author_badges_synthesized():
    item = _item(authorDetails={
        "channelId": "UCaaa", "displayName": "Owner",
        "isChatOwner": True, "isChatModerator": True,
        "isVerified": True, "isChatSponsor": True})
    badges = map_item(item).author.badges
    assert {(b.set_id, b.version_id) for b in badges} == {
        ("yt", "owner"), ("yt", "moderator"), ("yt", "verified"), ("yt", "member")}


def test_missing_author_yields_none():
    item = _item()
    del item["authorDetails"]
    assert map_item(item).author is None


def test_missing_author_details_falls_back_to_channel_id():
    # gRPC streamList sends no authorDetails — only snippet.authorChannelId.
    item = _item()
    del item["authorDetails"]
    item["snippet"]["authorChannelId"] = "UCaaa"
    msg = map_item(item)
    assert msg.author is not None
    assert msg.author.name == "UCaaa"
    assert msg.author.user_id == "UCaaa"
    assert not msg.is_system


def test_pb2_text_without_author_details_uses_channel_id():
    snippet = pb.LiveChatMessageSnippet(
        type=pb.LiveChatMessageSnippet.TypeWrapper.TEXT_MESSAGE_EVENT,
        live_chat_id="Cg0KC2Zvbw",
        author_channel_id="UCzzz",
        has_display_content=True,
        display_message="hi",
        text_message_details=pb.LiveChatTextMessageDetails(message_text="hi"),
    )
    msg = map_item(pb2_to_item(pb.LiveChatMessage(id="pb-x", snippet=snippet)))
    assert msg.author is not None
    assert msg.author.user_id == "UCzzz"
    assert msg.flags == MessageFlags.NONE
    assert not msg.is_system


# -- pb2_to_item ----------------------------------------------------------------------


def test_pb2_to_item_round_trip():
    item = pb2_to_item(_pb_item())
    assert item["id"] == "pb-1"
    snippet = item["snippet"]
    assert snippet["type"] == "textMessageEvent"
    assert snippet["liveChatId"] == "Cg0KC2Zvbw"
    assert snippet["textMessageDetails"]["messageText"] == "hello"
    assert snippet["publishedAt"] == "2026-10-15T10:00:00Z"
    assert item["authorDetails"]["displayName"] == "Alice"
    # and it maps through map_item unchanged
    assert map_item(item).plain == "hello"


def test_pb2_to_item_ban_details():
    snippet = pb.LiveChatMessageSnippet(
        type=pb.LiveChatMessageSnippet.TypeWrapper.USER_BANNED_EVENT,
        live_chat_id="Cg0KC2Zvbw",
        user_banned_details=pb.LiveChatUserBannedMessageDetails(
            ban_type=pb.LiveChatUserBannedMessageDetails.BanTypeWrapper.PERMANENT,
        ),
    )
    snippet.user_banned_details.banned_user_details.display_name = "Troll"
    item = pb2_to_item(pb.LiveChatMessage(id="pb-2", snippet=snippet))
    details = item["snippet"]["userBannedDetails"]
    assert details["banType"] == "permanent"
    assert details["bannedUserDetails"]["displayName"] == "Troll"


def test_pb2_to_item_supersticker_metadata():
    snippet = pb.LiveChatMessageSnippet(
        type=pb.LiveChatMessageSnippet.TypeWrapper.SUPER_STICKER_EVENT,
        live_chat_id="Cg0KC2Zvbw",
        super_sticker_details=pb.LiveChatSuperStickerDetails(
            amount_micros=2000000, currency="USD",
        ),
    )
    snippet.super_sticker_details.super_sticker_metadata.alt_text = "Party popper"
    item = pb2_to_item(pb.LiveChatMessage(id="pb-3", snippet=snippet))
    sticker = item["snippet"]["superStickerDetails"]
    assert sticker["superStickerMetadata"]["altText"] == "Party popper"


def test_pb2_superchat_round_trips_comment():
    snippet = pb.LiveChatMessageSnippet(
        type=pb.LiveChatMessageSnippet.TypeWrapper.SUPER_CHAT_EVENT,
        live_chat_id="Cg0KC2Zvbw",
        display_message="$5.00",
        super_chat_details=pb.LiveChatSuperChatDetails(
            amount_micros=5000000, currency="USD",
            amount_display_string="$5.00", user_comment="great stream"),
    )
    item = pb2_to_item(pb.LiveChatMessage(id="pb-4", snippet=snippet))
    details = item["snippet"]["superChatDetails"]
    assert details["userComment"] == "great stream"
    assert details["amountDisplayString"] == "$5.00"
    msg = map_item(item)
    assert msg.flags & MessageFlags.SUPERCHAT
    assert msg.plain == "great stream"
    assert msg.extra["superchat"]["amountMicros"] == 5000000


def test_pb2_supersticker_round_trips_alt_text():
    snippet = pb.LiveChatMessageSnippet(
        type=pb.LiveChatMessageSnippet.TypeWrapper.SUPER_STICKER_EVENT,
        live_chat_id="Cg0KC2Zvbw",
        display_message="$2.00",
        super_sticker_details=pb.LiveChatSuperStickerDetails(
            amount_micros=2000000, currency="USD", amount_display_string="$2.00",
        ),
    )
    snippet.super_sticker_details.super_sticker_metadata.alt_text = "Party popper"
    item = pb2_to_item(pb.LiveChatMessage(id="pb-5", snippet=snippet))
    msg = map_item(item)
    assert msg.flags & MessageFlags.SUPERCHAT
    assert msg.plain == "$2.00"
    assert msg.extra["sticker_alt"] == "Party popper"


def test_pb2_banned_round_trips_display_name():
    snippet = pb.LiveChatMessageSnippet(
        type=pb.LiveChatMessageSnippet.TypeWrapper.USER_BANNED_EVENT,
        live_chat_id="Cg0KC2Zvbw",
        user_banned_details=pb.LiveChatUserBannedMessageDetails(
            ban_type=pb.LiveChatUserBannedMessageDetails.BanTypeWrapper.PERMANENT,
        ),
    )
    snippet.user_banned_details.banned_user_details.display_name = "Troll"
    item = pb2_to_item(pb.LiveChatMessage(id="pb-6", snippet=snippet))
    msg = map_item(item)
    assert msg.flags & MessageFlags.SYSTEM
    assert msg.plain == "Troll was removed from chat by a moderator"


def test_pb2_type_map_covers_all_enum_values():
    enum = pb.LiveChatMessageSnippet.TypeWrapper.DESCRIPTOR.enum_types_by_name["Type"]
    for value in enum.values:
        if value.name == "INVALID_TYPE":
            continue
        assert value.number in PB2_TO_REST_TYPE, value.name
