"""Event-level mapping: parsed Twitch IRC lines -> app events.

The mapper is pure (no network): badge images, emote names and third-party
emotes are resolved through injected callbacks, so the whole PRIVMSG/USERNOTICE
surface is unit-testable.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...core.text import URL_RE, emote_tokens, parse_emotes_tag
from ...models.emote import Emote
from ...models.message import Author, Badge, ImageRun, Message, MessageFlags, Platform, TextRun

# -- lookups (injected) --------------------------------------------------------

BadgeLookup = Callable[[str, str], Badge | None]
EmoteNameLookup = Callable[[str, str | None], str | None]  # (twitch emote id, channel) -> name
ThirdPartyLookup = Callable[[str, str | None], Emote | None]  # (token, channel) -> 7TV/BTTV/FFZ emote

CHEER_RE = re.compile(r"\bCheer(\d+)\b")
ACTION_PREFIX = "\x01ACTION "
NOTICE_AUTH_FAILED = {"Login authentication failed", "Invalid NICK",
                      "Error logging in", "Login unsuccessful",
                      "Improperly formatted auth"}

SUB_MSG_IDS = {"sub", "resub", "subgift", "anonsubgift", "submysterygift", "giftpaidupgrade",
               "anongiftpaidupgrade", "primepaidupgrade", "primecommunitygiftuser",
               "standardpayforward", "communitypayforward", "rewardgift"}

# Friendly text for NOTICE msg-ids (Twitch moderation/send feedback). Success
# notices (timeout_success, ban_success, slow_on, ...) keep Twitch's own wording.
NOTICE_FRIENDLY = {
    "msg_rejected": "Your message was rejected by Twitch.",
    "msg_rejected_mandatory": "Your message wasn't posted — it conflicts with the channel's moderation settings.",
    "msg_duplicate": "Your message wasn't sent — it's identical to the previous one you sent.",
    "msg_timedout": "You are timed out from this channel.",
    "msg_ratelimit": "You are sending messages too fast — try again in a few seconds.",
    "msg_verified_email": "A verified email address is required to chat in this channel.",
    "msg_requires_verified_phone_number": "A verified phone number is required to chat in this channel.",
    "msg_censored_broadcaster": "Your message wasn't sent — the channel has blocked some of its words.",
    "msg_banned_words": "Your message wasn't sent — the channel has blocked some of its words.",
    "msg_bad_characters": "Your message wasn't sent — it contains characters Twitch doesn't allow.",
    "msg_channel_blocked": "This channel is blocked.",
    "msg_channel_suspended": "This channel has been suspended.",
    "msg_emoteonly": "This room is in emote-only mode.",
    "msg_slowmode": "This room is in slow mode.",
    "msg_subsonly": "This room is in subscribers-only mode.",
    "msg_followersonly": "This room is in followers-only mode.",
    "msg_r9k": "This room is in unique-chat (R9K) mode.",
    "msg_uniquechat": "This room is in unique-chat (R9K) mode.",
    "no_permission": "You don't have permission to perform that action.",
    "msg_no_permission": "You don't have permission to perform that action.",
    "msg_cmd_rejected": "Twitch rejected your command.",
    "bad_delete_message_error": "You can't delete that message.",
    "bad_timeout_admin": "You can't time out that user.",
    "bad_timeout_self": "You can't time out yourself.",
    "bad_timeout_duration": "Invalid timeout duration.",
    "bad_timeout_banned": "That user is banned from this channel.",
    "bad_ban_admin": "You can't ban that user.",
    "bad_ban_self": "You can't ban yourself.",
    "bad_ban_broadcaster": "You can't ban the broadcaster.",
    "bad_unban_no_ban": "That user isn't banned from this channel.",
    "bad_untimeout_no_timeout": "That user isn't timed out in this channel.",
    "already_banned": "That user is already banned from this channel.",
    "already_timedout": "That user is already timed out in this channel.",
    "bad_mod_mod": "That user is already a moderator.",
    "bad_mod_banned": "You can't mod a banned user.",
    "bad_unmod_mod": "That user is not a moderator.",
    "bad_vip_grantee_banned": "You can't VIP a banned user.",
    "bad_unvip_grantee_not_vip": "That user is not a VIP.",
    "bad_slow_duration": "Invalid slow-mode duration.",
    "bad_commercial_error": "You can't run a commercial right now.",
    "raid_error_self": "You can't raid yourself.",
    "raid_error_already_raiding": "You are already raiding another channel.",
    "raid_error_forbidden": "You can't raid right now.",
    "unraid_error_not_raiding": "There is no raid to cancel.",
    "unrecognized_cmd": "Twitch didn't recognize that command.",
    "no_help": "No help is available for that command.",
}


# -- events ---------------------------------------------------------------------

@dataclass
class ClearChat:
    """CLEARCHAT: `user` None means the whole chat was cleared."""

    channel: str
    user: str | None = None
    duration: int | None = None  # seconds; None = permanent ban


@dataclass
class ClearMsg:
    channel: str
    target_msg_id: str
    login: str


@dataclass
class RoomState:
    channel: str
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthFailed:
    reason: str


@dataclass
class Reconnect:
    pass


@dataclass
class Names:
    channel: str
    names: list[str] = field(default_factory=list)


@dataclass
class UserState:
    """USERSTATE: the connected account's own state in a channel.

    `mod` is True when the account is a moderator (or the broadcaster) there —
    the driver for moderation mode's per-channel shield.
    """

    channel: str
    mod: bool = False
    badges: list[Badge] = field(default_factory=list)


# -- tag helpers -----------------------------------------------------------------

def split_prefix(prefix: str | None) -> tuple[str, str, str]:
    """'nick!user@host' -> (nick, user, host)."""
    if not prefix:
        return "", "", ""
    nick, _, rest = prefix.partition("!")
    user, _, host = rest.partition("@")
    return nick, user, host


def parse_badges(tags: dict[str, str], badge_lookup: BadgeLookup | None = None) -> list[Badge]:
    badges: list[Badge] = []
    for pair in tags.get("badges", "").split(","):
        if not pair:
            continue
        set_id, _, version_id = pair.partition("/")
        if not version_id:
            continue
        if badge_lookup is not None:
            resolved = badge_lookup(set_id, version_id)
            if resolved is not None:
                badges.append(resolved)
                continue
        badges.append(Badge(
            set_id=set_id,
            version_id=version_id,
            title=set_id,
            # Helix data supplies the image URLs too (a per-version GUID keyed
            # on static-cdn.jtvnw.net); the IRC tag's set/version pair alone
            # can't be mapped to an image without Helix, so leave urls empty
            # and let the delegate show a labelled placeholder instead.
            urls={},
        ))
    return badges


def author_from_tags(
    tags: dict[str, str], badge_lookup: BadgeLookup | None = None, nick: str = ""
) -> Author:
    """Author from IRCv3 tags; `nick` (from the prefix) is the fallback for
    messages that carry no `login`/`display-name` tags."""
    name = tags.get("display-name") or tags.get("login") or nick
    color = tags.get("color")
    if color:
        color = f"#{color}" if not color.startswith("#") else color
    return Author(
        name=name,
        login=(tags.get("login") or nick).lower(),
        user_id=tags.get("user-id", ""),
        color=color,
        badges=parse_badges(tags, badge_lookup),
    )


def msg_timestamp(tags: dict[str, str]) -> dt.datetime:
    raw = tags.get("tmi-sent-ts", "")
    try:
        return dt.datetime.fromtimestamp(int(raw) / 1000, tz=dt.UTC).astimezone()
    except (ValueError, OSError):
        return dt.datetime.now()


def action_body(text: str) -> str:
    """Strip the CTCP ACTION wrapper, if present."""
    if text.startswith(ACTION_PREFIX) and text.endswith("\x01"):
        return text[len(ACTION_PREFIX):-1]
    return text


def cheer_amount(text: str) -> int | None:
    match = CHEER_RE.search(text)
    return int(match.group(1)) if match else None


def twitch_emote_urls(emote_id: str, theme: str = "dark") -> dict[str, str]:
    """Twitch v2 emoticon CDN template (public, no auth)."""
    base = f"https://static-cdn.jtvnw.net/emoticons/v2/{emote_id}/default/{theme}"
    return {"1x": f"{base}/1.0", "2x": f"{base}/2.0", "3x": f"{base}/3.0", "4x": f"{base}/3.0"}


# -- run building ----------------------------------------------------------------

def build_runs(
    text: str,
    twitch_ranges: list[tuple[str, int, int]],
    *,
    emote_name_lookup: EmoteNameLookup | None = None,
    third_party_lookup: ThirdPartyLookup | None = None,
    emote_urls: Callable[[str], dict[str, str]] | None = None,
    theme: str = "dark",
    channel: str | None = None,
) -> list:
    """Build render runs from text + parsed Twitch emote ranges.

    Twitch ranges are authoritative; the gaps between them are tokenized on
    whitespace and checked for URLs and third-party emotes. `channel` (the
    message's channel) is passed to the lookups so scoped emotes resolve
    against that channel's sets.
    """
    runs: list = []
    pos = 0
    for emote_id, start, end in twitch_ranges:
        if start > pos:
            runs.extend(_gap_runs(text[pos:start], third_party_lookup, channel))
        if start < pos or end > start:  # skip zero-length/broken ranges
            name = (emote_name_lookup(emote_id, channel) if emote_name_lookup else None) or text[start:end]
            urls = emote_urls(emote_id) if emote_urls else {}
            runs.append(ImageRun(Emote(emote_id, name, "twitch", urls=urls, height_px=28), name))
        pos = max(pos, end)
    if pos < len(text):
        runs.extend(_gap_runs(text[pos:], third_party_lookup, channel))
    return runs


def _gap_runs(
    gap: str, third_party_lookup: ThirdPartyLookup | None, channel: str | None = None
) -> list:
    runs: list = []
    for token in re.split(r"(\s+)", gap):
        if not token:
            continue
        if token.isspace():
            runs.append(TextRun(token))
            continue
        match = URL_RE.fullmatch(token) or URL_RE.fullmatch(token.rstrip(".,!?;:()[]{}<>\"'"))
        if match:
            runs.append(TextRun(token, link=token))
            continue
        if third_party_lookup is not None:
            for candidate in emote_tokens(token):
                emote = third_party_lookup(candidate, channel)
                if emote is not None:
                    runs.append(ImageRun(emote, emote.name))
                    break
            else:
                runs.append(TextRun(token))
        else:
            runs.append(TextRun(token))
    return runs


# -- the mapper -------------------------------------------------------------------

class TwitchMapper:
    def __init__(
        self,
        *,
        badge_lookup: BadgeLookup | None = None,
        emote_name_lookup: EmoteNameLookup | None = None,
        third_party_lookup: ThirdPartyLookup | None = None,
        emote_urls: Callable[[str], dict[str, str]] | None = None,
        self_user_id: str | None = None,
        theme: str = "dark",
    ):
        self.badge_lookup = badge_lookup
        self.emote_name_lookup = emote_name_lookup
        self.third_party_lookup = third_party_lookup
        self.emote_urls = emote_urls
        self.self_user_id = self_user_id
        self.theme = theme

    def map(self, irc, channel: str):
        """Route one IrcMessage to an event for `channel`."""
        cmd = irc.command
        if cmd == "PRIVMSG":
            return self._prvmsg(irc, channel)
        if cmd == "USERNOTICE":
            return self._usernotice(irc, channel)
        if cmd == "NOTICE":
            return self._notice(irc, channel)
        if cmd == "CLEARCHAT":
            return self._clearchat(irc, channel)
        if cmd == "CLEARMSG":
            return self._clearmsg(irc, channel)
        if cmd == "ROOMSTATE":
            return self._roomstate(irc, channel)
        if cmd == "USERSTATE":
            return self._userstate(irc, channel)
        if cmd == "RECONNECT":
            return Reconnect()
        if cmd == "353":
            return self._names(irc, channel)
        return None

    def parse_self_badges(self, tags: dict[str, str]) -> list[Badge]:
        """Badges for the connected account (GLOBALUSERSTATE/USERSTATE)."""
        return parse_badges(tags, self.badge_lookup)

    # -- individual commands ------------------------------------------------------

    def _prvmsg(self, irc, channel: str) -> Message | None:
        tags = irc.tags
        text = irc.params[-1] if len(irc.params) >= 2 else ""
        if not text:
            return None
        nick = split_prefix(irc.prefix)[0]
        author = author_from_tags(tags, self.badge_lookup, nick)
        is_action = text.startswith(ACTION_PREFIX) and text.endswith("\x01")
        body = action_body(text)
        # parse_emotes_tag returns indices into the ORIGINAL text (it accounts
        # for the ACTION prefix itself); shift them back onto `body`.
        ranges = parse_emotes_tag(text, tags.get("emotes", ""))
        if is_action:
            shift = len(ACTION_PREFIX)
            ranges = [(eid, max(0, s - shift), max(0, e - shift)) for eid, s, e in ranges]
        runs = build_runs(
            body,
            ranges,
            emote_name_lookup=self.emote_name_lookup,
            third_party_lookup=self.third_party_lookup,
            emote_urls=self.emote_urls,
            theme=self.theme,
            channel=channel.lstrip("#").lower(),
        )
        flags = MessageFlags.NONE
        extra: dict[str, Any] = {}
        if is_action:
            flags |= MessageFlags.ACTION
        if tags.get("room-id"):
            extra["room_id"] = tags["room-id"]
        if tags.get("first-msg") == "1":
            flags |= MessageFlags.FIRST_MESSAGE
            extra["first_message"] = True
        if tags.get("reply-parent-msg-id"):
            flags |= MessageFlags.REPLY
        amount = cheer_amount(body)
        if amount is not None:
            flags |= MessageFlags.CHEER
            extra["bits"] = amount
        msg = Message(
            id=tags.get("id", ""),
            platform=Platform.TWITCH,
            channel_id=channel,
            ts=msg_timestamp(tags),
            author=author,
            runs=runs,
            plain=body,
            flags=flags,
            reply_parent=tags.get("reply-parent-msg-id"),
            reply_parent_name=tags.get("reply-parent-display-name"),
            extra=extra,
        )
        return msg

    def _usernotice(self, irc, channel: str) -> Message:
        tags = irc.tags
        body = irc.params[-1] if len(irc.params) >= 2 else ""
        msg_id = tags.get("msg-id", "")
        system_msg = tags.get("system-msg", "")
        nick = split_prefix(irc.prefix)[0]
        author = author_from_tags(tags, self.badge_lookup, nick)
        plain = body or system_msg
        flags = MessageFlags.SYSTEM
        extra: dict[str, Any] = {"msg_id": msg_id}
        if tags.get("room-id"):
            extra["room_id"] = tags["room-id"]

        if msg_id in SUB_MSG_IDS:
            flags = MessageFlags.SUBSCRIPTION
            extra.update({
                "months": tags.get("msg-param-cumulative-months", ""),
                "streak": tags.get("msg-param-streak-months", ""),
                "plan": tags.get("msg-param-sub-plan", ""),
                "recipient": tags.get("msg-param-recipient-display-name", ""),
                "gift_count": tags.get("msg-param-mass-gift-count", ""),
            })
            # Gift rows highlight the RECIPIENT, not the sender.
            if msg_id in {"subgift", "anonsubgift"}:
                author = Author(
                    name=tags.get("msg-param-recipient-display-name", ""),
                    login=tags.get("msg-param-recipient-user-name", "").lower(),
                    user_id=tags.get("msg-param-recipient-id", ""),
                )
                extra["sender"] = tags.get("display-name", "")
        elif msg_id == "announcement":
            flags = MessageFlags.ANNOUNCEMENT
            extra["color"] = tags.get("msg-param-color", "").lower()
        elif msg_id == "raid":
            flags = MessageFlags.SYSTEM
            extra["viewers"] = tags.get("msg-param-viewerCount", "")
            author = None
        elif msg_id == "ritual":
            flags = MessageFlags.FIRST_MESSAGE
        elif msg_id == "bitsbadgetier":
            flags = MessageFlags.CHEER
            extra["bits"] = tags.get("msg-param-threshold", "")

        runs = [TextRun(plain)]
        msg = Message(
            id=tags.get("id", ""),
            platform=Platform.TWITCH,
            channel_id=channel,
            ts=msg_timestamp(tags),
            author=author,
            runs=runs,
            plain=plain,
            flags=flags,
            extra=extra,
        )
        return msg

    def _notice(self, irc, channel: str) -> Message | AuthFailed:
        text = irc.params[-1] if len(irc.params) >= 2 else ""
        # Only the connection-level failure texts are AuthFailed; msg-id
        # notices (msg_banned = a per-channel ban, msg_bad_auth) are
        # channel-scoped and render as system rows where they belong.
        if text in NOTICE_AUTH_FAILED:
            return AuthFailed(text)
        msg_id = irc.tags.get("msg-id", "")
        msg = Message.system(
            Platform.TWITCH,
            channel,
            NOTICE_FRIENDLY.get(msg_id, text),
            ts=msg_timestamp(irc.tags),
            extra={"msg_id": msg_id},
        )
        return msg

    def _clearchat(self, irc, channel: str) -> ClearChat:
        user = irc.params[-1] if len(irc.params) >= 2 else None
        duration_raw = irc.tags.get("ban-duration", "")
        duration = int(duration_raw) if duration_raw.isdigit() else None
        return ClearChat(channel=channel, user=user or None, duration=duration)

    def _clearmsg(self, irc, channel: str) -> ClearMsg:
        return ClearMsg(
            channel=channel,
            target_msg_id=irc.tags.get("target-msg-id", ""),
            login=irc.tags.get("login", ""),
        )

    def _roomstate(self, irc, channel: str) -> RoomState:
        tags = irc.tags
        followers_raw = tags.get("followers-only", "")
        # "-1"/"" = disabled; "0" = enabled with no minimum.
        followers_only = int(followers_raw) if followers_raw.isdigit() else -1
        state = {
            "emote_only": tags.get("emote-only") == "1",
            "subs_only": tags.get("subs-only") == "1",
            "r9k": tags.get("r9k") == "1",
            "rituals": tags.get("rituals") == "1",
            "slow": int(tags["slow"]) if tags.get("slow", "").isdigit() else 0,
            "followers_only": followers_only,
            "room_id": tags.get("room-id", ""),
        }
        return RoomState(channel=channel, state=state)

    def _names(self, irc, channel: str) -> Names:
        names = (irc.params[-1] if irc.params else "").split()
        return Names(channel=channel, names=names)

    def _userstate(self, irc, channel: str) -> UserState:
        tags = irc.tags
        mod = tags.get("mod") == "1" or tags.get("user-type") == "mod"
        return UserState(
            channel=channel,
            mod=mod,
            badges=parse_badges(tags, self.badge_lookup),
        )
