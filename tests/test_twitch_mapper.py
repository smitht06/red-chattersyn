"""TwitchMapper: PRIVMSG/USERNOTICE/NOTICE/CLEARCHAT/CLEARMSG/ROOMSTATE mapping."""

import datetime as dt

from chattersyn.models.emote import Emote
from chattersyn.models.message import Badge, ImageRun, MessageFlags, Platform, TextRun
from chattersyn.services.twitch import parse
from chattersyn.services.twitch.mapper import (
    NOTICE_FRIENDLY,
    AuthFailed,
    ClearChat,
    ClearMsg,
    Names,
    Reconnect,
    RoomState,
    TwitchMapper,
    UserState,
    action_body,
    build_runs,
    cheer_amount,
)

PRIVMSG = (
    "@badge-info=;badges=broadcaster/1;color=#FF0000;display-name=Forsen;"
    "emotes=25:0-4,10-14/1902:6-8;first-msg=0;flags=;id=msg-id-1;mod=0;"
    "room-id=22484632;subscriber=0;tmi-sent-ts=1620000000000;turbo=0;"
    "user-id=22484632;user-type= :forsen!forsen@forsen.tmi.twitch.tv "
    "PRIVMSG #forsen :Kappa hey Kappa there"
)


def _map(line, channel="#forsen", **kwargs):
    irc = parse.parse_irc_line(line)
    assert irc is not None
    return TwitchMapper(**kwargs).map(irc, channel)


# -- PRIVMSG ------------------------------------------------------------------------


def test_prvmsg_basic():
    msg = _map(PRIVMSG)
    assert msg is not None
    assert msg.platform is Platform.TWITCH
    assert msg.id == "msg-id-1"
    assert msg.author.name == "Forsen"
    assert msg.author.login == "forsen"
    assert msg.author.color == "#FF0000"
    assert msg.plain == "Kappa hey Kappa there"
    assert msg.ts == dt.datetime.fromtimestamp(1620000000).astimezone()
    # runs: [Kappa][space][hey][space][Kappa][space][there]
    assert len(msg.runs) == 7
    assert isinstance(msg.runs[0], ImageRun) and msg.runs[0].emote.id == "25"
    assert isinstance(msg.runs[2], ImageRun) and msg.runs[2].emote.id == "1902"
    assert isinstance(msg.runs[4], ImageRun) and msg.runs[4].emote.id == "25"
    assert isinstance(msg.runs[6], TextRun) and msg.runs[6].text == "there"


def test_prvmsg_third_party_emote_and_url():
    line = (
        "@badges=;color=;display-name=Viewer;emotes=;flags=;id=m2;mod=0;"
        "room-id=1;subscriber=0;tmi-sent-ts=1620000000000;turbo=0;user-id=2;user-type= "
        ":viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #forsen "
        ":KEKW check https://example.com/x KEKW!"
    )
    seven_tv = {"KEKW": Emote("k1", "KEKW", "7tv", urls={"1x": "https://cdn.7tv.app/emote/k1/1x.webp"})}
    msg = _map(line, third_party_lookup=lambda token, channel=None: seven_tv.get(token))
    assert msg is not None
    assert any(isinstance(r, ImageRun) and r.emote.provider == "7tv" for r in msg.runs)
    assert any(isinstance(r, TextRun) and r.link == "https://example.com/x" for r in msg.runs)
    # trailing "KEKW!" still matches the stripped token
    assert sum(1 for r in msg.runs if isinstance(r, ImageRun)) == 2


def test_prvmsg_lookups_receive_the_channel():
    line = (
        "@badges=;color=;display-name=Viewer;emotes=25:0-4;flags=;id=m3;mod=0;"
        "room-id=1;subscriber=0;tmi-sent-ts=1620000000000;turbo=0;user-id=2;user-type= "
        ":viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #Alice :Kappa KEKW"
    )
    seen: dict[str, list[str | None]] = {"name": [], "party": []}
    msg = _map(
        line,
        channel="#Alice",
        emote_name_lookup=lambda eid, channel=None: seen["name"].append(channel) or "Kappa",
        third_party_lookup=lambda token, channel=None: seen["party"].append(channel) or None,
    )
    assert msg is not None
    # Both lookups see the normalized channel (lowercased, no #).
    assert seen["name"] == ["alice"]
    assert seen["party"] and all(c == "alice" for c in seen["party"])


def test_prvmsg_action_strips_wrapper_and_shifts_ranges():
    line = (
        "@badges=;color=#00FF00;display-name=Nightbot;emotes=28087:14-15;flags=;id=m3;mod=0;"
        "room-id=1;subscriber=0;tmi-sent-ts=1620000000000;turbo=0;user-id=3;user-type= "
        ":nightbot!nightbot@nightbot.tmi.twitch.tv PRIVMSG #forsen "
        ":\x01ACTION waves 😀 hi\x01"
    )
    msg = _map(line)
    assert msg is not None
    assert msg.flags & MessageFlags.ACTION
    assert msg.plain == "waves 😀 hi"
    # The emote range covers the astral char: full text "\x01ACTION waves 😀 hi\x01"
    # puts 😀 at UTF-16 units 14-15 (the prefix is 8 units); on the body it is
    # Python index 6, so the range must have been shifted back by 8.
    assert msg.runs[2].emote.id == "28087"  # "waves", " ", then the emote


def test_prvmsg_reply_tags():
    line = (
        "@badges=moderator/1;color=;display-name=ModUser;emotes=;flags=;id=m4;mod=1;"
        "reply-parent-display-name=OriginalPoster;reply-parent-msg-id=parent-1;"
        "reply-parent-user-login=originalposter;room-id=1;subscriber=0;"
        "tmi-sent-ts=1620000000000;turbo=0;user-id=4;user-type=mod "
        ":moduser!moduser@moduser.tmi.twitch.tv PRIVMSG #forsen :agreed!"
    )
    msg = _map(line)
    assert msg is not None
    assert msg.flags & MessageFlags.REPLY
    assert msg.reply_parent == "parent-1"
    assert msg.reply_parent_name == "OriginalPoster"


def test_prvmsg_first_msg_and_cheer():
    line = (
        "@badges=;color=#FF69B4;display-name=Cheerer;emotes=;first-msg=1;flags=;id=m5;"
        "mod=0;room-id=1;subscriber=0;tmi-sent-ts=1620000000000;turbo=0;"
        "user-id=5;user-type= :cheerer!cheerer@cheerer.tmi.twitch.tv "
        "PRIVMSG #forsen :Cheer100 let's go!"
    )
    msg = _map(line)
    assert msg is not None
    assert msg.flags & MessageFlags.FIRST_MESSAGE
    assert msg.flags & MessageFlags.CHEER
    assert msg.extra["bits"] == 100


def test_prvmsg_badges_resolved_via_lookup():
    def badge_lookup(set_id, version_id):
        if set_id == "broadcaster":
            return Badge(set_id, version_id, "Broadcaster", urls={"1x": "https://x/b.png"})
        return None

    msg = _map(PRIVMSG, badge_lookup=badge_lookup)
    assert msg is not None
    assert msg.author.badges[0].title == "Broadcaster"
    assert msg.author.badges[0].url_for() == "https://x/b.png"


# -- USERNOTICE ---------------------------------------------------------------------


def test_usernotice_sub():
    line = (
        "@badge-info=subscriber/30;badges=subscriber/30;color=;display-name=Test;emotes=;"
        "flags=;id=u1;login=test;mod=0;msg-id=sub;msg-param-cumulative-months=12;"
        "msg-param-sub-plan-name=Channel\\sSubscription\\s(twitch.tv);"
        "msg-param-sub-plan=Prime;room-id=1;subscriber=1;"
        "system-msg=Test\\ssubscribed\\swith\\sPrime.\\sThey've\\ssubscribed\\sfor\\s12\\smonths!;"
        "tmi-sent-ts=1620000000000;user-id=6;user-type= "
        ":tmi.twitch.tv USERNOTICE #forsen :thanks for the sub!"
    )
    msg = _map(line)
    assert msg is not None
    assert msg.flags & MessageFlags.SUBSCRIPTION
    assert msg.plain == "thanks for the sub!"
    assert msg.extra["plan"] == "Prime"
    assert msg.extra["months"] == "12"


def test_usernotice_subgift_highlights_recipient():
    line = (
        "@badges=;color=;display-name=Gifter;emotes=;flags=;id=u2;login=gifter;mod=0;"
        "msg-id=subgift;msg-param-recipient-display-name=Recipient;"
        "msg-param-recipient-id=99;msg-param-recipient-user-name=recipient;"
        "msg-param-sub-plan=1000;room-id=1;subscriber=0;"
        "system-msg=Gifter\\sgifted\\sa\\sTier\\s1\\ssub\\sto\\sRecipient!;"
        "tmi-sent-ts=1620000000000;user-id=8;user-type= "
        ":tmi.twitch.tv USERNOTICE #forsen"
    )
    msg = _map(line)
    assert msg is not None
    assert msg.author.name == "Recipient"
    assert msg.author.login == "recipient"
    assert msg.extra["sender"] == "Gifter"


def test_usernotice_announcement():
    line = (
        "@badges=staff/1;color=#FF00FF;display-name=StaffGuy;emotes=;flags=;id=u3;"
        "login=staffguy;mod=0;msg-id=announcement;msg-param-color=BLUE;room-id=1;"
        "subscriber=0;system-msg=;tmi-sent-ts=1620000000000;user-id=12;user-type=staff "
        ":tmi.twitch.tv USERNOTICE #forsen :this is an announcement"
    )
    msg = _map(line)
    assert msg is not None
    assert msg.flags & MessageFlags.ANNOUNCEMENT
    assert msg.extra["color"] == "blue"
    assert msg.plain == "this is an announcement"


def test_usernotice_raid_is_system_row():
    line = (
        "@badges=;color=;display-name=RaidLeader;emotes=;flags=;id=u4;login=raidleader;"
        "mod=0;msg-id=raid;msg-param-viewerCount=152;room-id=1;subscriber=0;"
        "system-msg=152\\sraiders\\sfrom\\sRaidLeader\\shave\\sjoined!;"
        "tmi-sent-ts=1620000000000;user-id=10;user-type= "
        ":tmi.twitch.tv USERNOTICE #forsen"
    )
    msg = _map(line)
    assert msg is not None
    assert msg.author is None
    assert msg.extra["viewers"] == "152"
    assert msg.plain == "152 raiders from RaidLeader have joined!"


# -- NOTICE / moderation --------------------------------------------------------------


def test_notice_auth_failed():
    msg = _map(":tmi.twitch.tv NOTICE #forsen :Login authentication failed")
    assert isinstance(msg, AuthFailed)


def test_notice_improperly_formatted_auth_is_auth_failed():
    """A rejected PASS must surface as an auth failure, not a system row
    with an empty channel that the router silently drops."""
    msg = _map(":tmi.twitch.tv NOTICE * :Improperly formatted auth")
    assert isinstance(msg, AuthFailed)


def test_notice_system_row():
    msg = _map("@msg-id=subs_on :tmi.twitch.tv NOTICE #forsen :This room is now in subscribers-only mode.")
    assert msg is not None
    assert msg.is_system
    assert msg.extra["msg_id"] == "subs_on"


def test_notice_msg_banned_is_channel_system_row():
    """A per-channel ban notice (msg_banned) is not a connection failure:
    it renders as a system row in the channel, never as AuthFailed.
    (irc.py strips the '#' before calling the mapper.)"""
    msg = _map("@msg-id=msg_banned :tmi.twitch.tv NOTICE #forsen "
               ":You are permanently banned from talking in forsen.",
               channel="forsen")
    assert msg is not None
    assert not isinstance(msg, AuthFailed)
    assert msg.is_system
    assert msg.platform is Platform.TWITCH
    assert msg.channel_id == "forsen"
    assert msg.extra["msg_id"] == "msg_banned"
    assert msg.plain == "You are permanently banned from talking in forsen."


def test_clearchat_timeout():
    ev = _map(
        "@ban-duration=600;room-id=1;target-user-id=99;tmi-sent-ts=1 "
        ":tmi.twitch.tv CLEARCHAT #forsen :badperson"
    )
    assert isinstance(ev, ClearChat)
    assert ev.user == "badperson" and ev.duration == 600


def test_clearchat_ban_and_clear():
    ev = _map("@room-id=1;target-user-id=99;tmi-sent-ts=1 :tmi.twitch.tv CLEARCHAT #forsen :bannedforever")
    assert isinstance(ev, ClearChat) and ev.duration is None and ev.user == "bannedforever"
    ev = _map("@room-id=1;tmi-sent-ts=1 :tmi.twitch.tv CLEARCHAT #forsen")
    assert isinstance(ev, ClearChat) and ev.user is None


def test_clearmsg():
    ev = _map("@login=deleteduser;target-msg-id=abc :tmi.twitch.tv CLEARMSG #forsen :whatever")
    assert isinstance(ev, ClearMsg)
    assert ev.target_msg_id == "abc" and ev.login == "deleteduser"


def test_roomstate():
    ev = _map(
        "@emote-only=1;followers-only=10;r9k=1;rituals=0;room-id=1;slow=15;subs-only=1 "
        ":tmi.twitch.tv ROOMSTATE #forsen"
    )
    assert isinstance(ev, RoomState)
    assert ev.state == {
        "emote_only": True, "subs_only": True, "r9k": True, "rituals": False,
        "slow": 15, "followers_only": 10, "room_id": "1",
    }
    ev = _map("@emote-only=0;followers-only=-1;r9k=0;rituals=1;room-id=1;slow=0;subs-only=0 "
              ":tmi.twitch.tv ROOMSTATE #forsen")
    assert ev.state["followers_only"] == -1


def test_names_and_reconnect():
    ev = _map(":tmi.twitch.tv 353 justinfan12345 = #forsen :a b c")
    assert isinstance(ev, Names) and ev.names == ["a", "b", "c"]
    assert isinstance(_map(":tmi.twitch.tv RECONNECT"), Reconnect)


# -- USERSTATE / moderation feedback ---------------------------------------------------


def test_userstate_mod_flag():
    ev = _map("@mod=1;room-id=1 :tmi.twitch.tv USERSTATE #forsen")
    assert isinstance(ev, UserState)
    assert ev.channel == "#forsen" and ev.mod is True


def test_userstate_user_type_mod():
    ev = _map("@user-type=mod;room-id=1 :tmi.twitch.tv USERSTATE #forsen")
    assert isinstance(ev, UserState) and ev.mod is True


def test_userstate_badges_parsed():
    ev = _map("@mod=0;badges=moderator/1,vip/1;room-id=1 :tmi.twitch.tv USERSTATE #forsen")
    assert isinstance(ev, UserState) and ev.mod is False
    assert {b.set_id for b in ev.badges} == {"moderator", "vip"}


def test_notice_mod_feedback_mapped_to_friendly_text():
    msg = _map("@msg-id=msg_rejected :tmi.twitch.tv NOTICE #forsen :whatever")
    assert msg is not None and msg.is_system
    assert msg.plain == NOTICE_FRIENDLY["msg_rejected"]
    assert msg.extra["msg_id"] == "msg_rejected"


def test_notice_unknown_msg_id_keeps_text():
    msg = _map("@msg-id=custom_thing :tmi.twitch.tv NOTICE #forsen :raw text here")
    assert msg is not None and msg.is_system
    assert msg.plain == "raw text here"


def test_room_id_captured_for_anonymous_emote_loading():
    # Without Helix credentials the service derives the numeric id from the
    # room-id tag to feed 7TV/BTTV/FFZ lookups.
    msg = _map(PRIVMSG)
    assert msg is not None and msg.extra["room_id"] == "22484632"
    sub = _map(
        "@login=subber;mod=0;msg-id=sub;msg-param-sub-plan=1000;"
        "room-id=22484632;subscriber=1;tmi-sent-ts=1620000000000;turbo=0;"
        "user-id=7;user-type= :subber!subber@subber.tmi.twitch.tv "
        "USERNOTICE #forsen :Subscribed!")
    assert sub.extra["room_id"] == "22484632"


def test_badge_fallback_has_no_url():
    msg = _map(PRIVMSG)  # no badge_lookup => anonymous run
    assert msg is not None
    badge = msg.author.badges[0]
    assert badge.set_id == "broadcaster"
    assert badge.urls == {}  # the IRC set/version can't map to an image without Helix
    assert badge.url_for() is None


# -- helpers ---------------------------------------------------------------------------


def test_action_body():
    assert action_body("\x01ACTION waves\x01") == "waves"
    assert action_body("plain text") == "plain text"


def test_cheer_amount():
    assert cheer_amount("Cheer100 go!") == 100
    assert cheer_amount("no bits here") is None


def test_build_runs_astral_url_token():
    runs = build_runs("😀 https://x.y/z", [])
    assert isinstance(runs[0], TextRun) and runs[0].text == "😀"
    assert runs[2].link == "https://x.y/z"
