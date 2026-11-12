"""OBS-9, OBS-21: chat rows become overlay events; app notes and removed rows don't."""

import datetime as dt

from chattersyn.models.emote import Emote
from chattersyn.models.message import (
    Author,
    Badge,
    ImageRun,
    Message,
    MessageFlags,
    Platform,
    TextRun,
)
from chattersyn.services.overlay_feed import message_to_event

TS = dt.datetime(2026, 11, 11, 14, 5, 9)


def _row(text="hello", *, platform=Platform.TWITCH, author=None, **kw):
    author = author if author is not None else Author(name="Bob", login="bob", color="#ff0000")
    return Message.text(platform, "alice", author, text, ts=TS, id=kw.pop("id", "m1"), **kw)


def test_normal_row():
    event = message_to_event(_row(), now=100.0)
    assert event == {
        "id": "m1", "platform": "twitch", "name": "Bob", "color": "#ff0000",
        "badges": [], "parts": [{"t": "text", "v": "hello"}], "ts": "14:05",
        "arrived": 100.0, "kind": "normal", "bar": None, "amount": "",
    }


def test_action_row():
    event = message_to_event(_row(flags=MessageFlags.ACTION))
    assert event["kind"] == "action"


def test_emote_and_badge_urls():
    emote = Emote(id="1", name="Kappa", provider="twitch",
                  urls={"1x": "https://cdn.test/k1.png", "2x": "https://cdn.test/k2.png"})
    author = Author(name="Bob", login="bob", badges=[
        Badge(set_id="sub", version_id="1", title="Subscriber",
              urls={"1x": "https://cdn.test/b1.png", "2x": "https://cdn.test/b2.png"}),
        Badge(set_id="yt", version_id="owner", title="Owner"),  # no art: dropped
    ])
    msg = _row(author=author)
    msg.runs = [TextRun("hi "), ImageRun(emote=emote, alt="Kappa")]
    event = message_to_event(msg)
    assert event["parts"] == [{"t": "text", "v": "hi "},
                              {"t": "emote", "url": "https://cdn.test/k2.png", "name": "Kappa"}]
    assert event["badges"] == [{"url": "https://cdn.test/b2.png", "title": "Subscriber"}]


def test_non_http_urls_dropped():
    emote = Emote(id="1", name="Evil", provider="7tv", urls={"1x": "javascript:alert(1)"})
    author = Author(name="Bob", login="bob", badges=[
        Badge(set_id="x", version_id="1", title="X", urls={"1x": "data:image/png;base64,AA"})])
    msg = _row(author=author)
    msg.runs = [ImageRun(emote=emote, alt="Evil")]
    event = message_to_event(msg)
    assert event["parts"] == [{"t": "text", "v": "Evil"}]
    assert event["badges"] == []


def test_twitch_sub_row():
    msg = _row("Bob subscribed for 3 months!", flags=MessageFlags.SUBSCRIPTION,
               extra={"msg_id": "resub"})
    event = message_to_event(msg)
    assert event["kind"] == "sub" and event["bar"] == "#00b3ff"


def test_announcement_colour():
    msg = _row("Stream soon", flags=MessageFlags.ANNOUNCEMENT,
               extra={"msg_id": "announcement", "color": "blue"})
    assert message_to_event(msg)["bar"] == "#1e90ff"


def test_raid_without_author():
    msg = Message.system(Platform.TWITCH, "alice", "carol is raiding with 12 viewers!",
                         ts=TS, id="r1", extra={"msg_id": "raid", "viewers": 12,
                                                          "usernotice": True})
    event = message_to_event(msg)
    assert event["kind"] == "raid" and event["name"] == ""
    assert event["parts"] == [{"t": "text", "v": "carol is raiding with 12 viewers!"}]


def test_youtube_superchat():
    msg = _row("$5.00 great stream", platform=Platform.YOUTUBE,
               author=Author(name="Dan", login="dan"),
               flags=MessageFlags.SUPERCHAT, extra={"youtube_type": "superChatEvent"})
    event = message_to_event(msg)
    assert event["platform"] == "youtube" and event["kind"] == "superchat"
    assert event["bar"] == "#f6c344" and event["color"] is None


def test_app_note_is_not_shown():
    """OBS-9: the app's own notes never reach the overlay."""
    note = Message.system(Platform.TWITCH, "alice", "Twitch connected", ts=TS)
    assert message_to_event(note) is None
    assert message_to_event(Message.system(Platform.SYSTEM, "", "Saved", ts=TS)) is None


def test_removed_rows_are_not_shown():
    assert message_to_event(_row(flags=MessageFlags.DELETED)) is None
    assert message_to_event(_row(flags=MessageFlags.DISABLED)) is None


def test_markup_stays_text():
    """OBS-21."""
    text = "<img src=x onerror=alert(1)>"
    assert message_to_event(_row(text))["parts"] == [{"t": "text", "v": text}]


# -- final-review fixes -----------------------------------------------------------------


def test_twitch_notice_is_not_shown():
    """Final review #1: NOTICE rows (errors, command feedback) carry a msg_id too,
    but they're the app's business, not the stream's."""
    for msg_id in ("msg_duplicate", "msg_ratelimit", "ban_success", "slow_on"):
        notice = Message.system(Platform.TWITCH, "alice", "You are sending messages too fast.",
                                ts=TS, extra={"msg_id": msg_id})
        assert message_to_event(notice) is None, msg_id


def test_twitch_usernotice_raid_shown():
    raid = Message.system(Platform.TWITCH, "alice", "carol is raiding!", ts=TS, id="r2",
                          extra={"msg_id": "raid", "usernotice": True})
    assert message_to_event(raid)["kind"] == "raid"


def test_youtube_moderation_notes_not_shown():
    """Final review #2: tombstones, removals, chat ended aren't shown on stream."""
    for kind in ("messageDeletedEvent", "userBannedEvent", "chatEndedEvent"):
        note = Message.system(Platform.YOUTUBE, "lc", "Bob was removed from chat", ts=TS,
                              extra={"youtube_type": kind})
        assert message_to_event(note) is None, kind


def test_youtube_utc_time_shown_in_local_time(monkeypatch):
    """Final review #3: YouTube times are UTC-aware; the page shows local time."""
    import os
    import time as time_mod
    if not hasattr(time_mod, "tzset"):
        return
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time_mod.tzset()
    try:
        msg = _row(platform=Platform.YOUTUBE, author=Author(name="Dan", login="dan"))
        msg.ts = dt.datetime(2026, 11, 11, 17, 5, tzinfo=dt.UTC)
        assert message_to_event(msg)["ts"] == "12:05"
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time_mod.tzset()


def test_superchat_amount_included():
    """D12: the Super Chat amount is shown even when there's a comment."""
    msg = _row("great stream", platform=Platform.YOUTUBE, author=Author(name="Dan", login="dan"),
               flags=MessageFlags.SUPERCHAT,
               extra={"youtube_type": "superChatEvent",
                      "superchat": {"amountDisplayString": "$5.00", "userComment": "great stream"}})
    assert message_to_event(msg)["amount"] == "$5.00"
