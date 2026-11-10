"""Twitch moderation and broadcaster commands, run through Helix.

`run_helix_command` makes the Helix call(s) for one parsed command and returns
the confirmation row (or a name list for /mods and /vips). Twitch failures and
unknown users raise `HelixError`; the caller formats them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from chattersyn.services.twitch import auth
from chattersyn.services.twitch.helix import Helix, HelixError

_ANNOUNCE_COLORS = {
    "announce": "primary", "announceblue": "blue", "announcegreen": "green",
    "announceorange": "orange", "announcepurple": "purple",
}

# name -> (chat-settings field, label used in the confirmation row)
_TOGGLES = {
    "emoteonly": ("emote_mode", "Emote-only"),
    "subscribers": ("subscriber_mode", "Subscribers-only"),
    "uniquechat": ("unique_chat_mode", "Unique-chat"),
    "r9kbeta": ("unique_chat_mode", "Unique-chat"),
}

DEFAULT_SLOW_SECONDS = 30
DEFAULT_COMMERCIAL_SECONDS = 30


def _build_scopes() -> dict[str, str | None]:
    scopes: dict[str, str | None] = {"uptime": None}
    for name in _ANNOUNCE_COLORS:
        scopes[name] = auth.ANNOUNCEMENTS_SCOPE
    for name in ("slow", "followers", *_TOGGLES):
        scopes[name] = auth.CHAT_SETTINGS_SCOPE
        scopes[name + "off"] = auth.CHAT_SETTINGS_SCOPE
    scopes["warn"] = auth.WARNINGS_SCOPE
    scopes["shoutout"] = auth.SHOUTOUTS_SCOPE
    scopes["shield"] = scopes["shieldoff"] = auth.SHIELD_SCOPE
    scopes["clear"] = scopes["delete"] = auth.CHAT_MESSAGES_SCOPE
    for name in ("mod", "unmod", "mods"):
        scopes[name] = auth.MODERATORS_SCOPE
    for name in ("vip", "unvip", "vips"):
        scopes[name] = auth.VIPS_SCOPE
    scopes["raid"] = scopes["unraid"] = auth.RAIDS_SCOPE
    scopes["commercial"] = auth.COMMERCIAL_SCOPE
    for name in ("marker", "settitle", "setgame"):
        scopes[name] = auth.BROADCAST_SCOPE
    return scopes


COMMAND_SCOPES: dict[str, str | None] = _build_scopes()


@dataclass
class HelixCommand:
    name: str  # the typed command, lowercase ("announceblue", "r9kbeta")
    user: str = ""
    text: str = ""
    seconds: int = 0  # always seconds; /followers converts to minutes
    message_id: str = ""


@dataclass
class HelixResult:
    row: str = ""
    names: list[str] | None = None


async def _user_id(helix: Helix, login: str) -> str:
    login = login.lstrip("@")
    user = await helix.user_by_login(login)
    if not user or not user.get("id"):
        raise HelixError(f"No Twitch user named {login}")
    return user["id"]


def _duration(started_at: str, now: datetime) -> str:
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return "0m"
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    minutes = max(0, int((now - started).total_seconds()) // 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


async def run_helix_command(helix: Helix, cmd: HelixCommand, *, channel: str,
                            broadcaster_id: str, moderator_id: str,
                            now: datetime) -> HelixResult:
    name = cmd.name
    b, m = broadcaster_id, moderator_id
    user = cmd.user.lstrip("@")

    if name in _ANNOUNCE_COLORS:
        await helix.send_announcement(b, m, cmd.text, _ANNOUNCE_COLORS[name])
        return HelixResult()
    if name == "slow":
        secs = cmd.seconds or DEFAULT_SLOW_SECONDS
        await helix.update_chat_settings(b, m, slow_mode=True, slow_mode_wait_time=secs)
        return HelixResult(f"Slow mode on ({secs} s).")
    if name == "slowoff":
        await helix.update_chat_settings(b, m, slow_mode=False)
        return HelixResult("Slow mode off.")
    if name == "followers":
        minutes = cmd.seconds // 60
        await helix.update_chat_settings(
            b, m, follower_mode=True, follower_mode_duration=minutes)
        when = f"{minutes} min" if minutes else "any follower"
        return HelixResult(f"Followers-only mode on ({when}).")
    if name == "followersoff":
        await helix.update_chat_settings(b, m, follower_mode=False)
        return HelixResult("Followers-only mode off.")
    base = name[:-3] if name.endswith("off") else name
    if base in _TOGGLES:
        field, label = _TOGGLES[base]
        on = base == name
        await helix.update_chat_settings(b, m, **{field: on})
        return HelixResult(f"{label} mode {'on' if on else 'off'}.")
    if name in ("shield", "shieldoff"):
        await helix.set_shield_mode(b, m, name == "shield")
        return HelixResult("Shield mode on." if name == "shield" else "Shield mode off.")
    if name == "clear":
        await helix.delete_chat_messages(b, m)
        return HelixResult()
    if name == "delete":
        await helix.delete_chat_messages(b, m, message_id=cmd.message_id)
        return HelixResult()
    if name == "warn":
        await helix.warn_user(b, m, await _user_id(helix, user), cmd.text)
        return HelixResult(f"You warned {user}.")
    if name in ("mod", "unmod", "vip", "unvip"):
        uid = await _user_id(helix, user)
        if name == "mod":
            await helix.add_moderator(b, uid)
            return HelixResult(f"You added {user} as a moderator.")
        if name == "unmod":
            await helix.remove_moderator(b, uid)
            return HelixResult(f"You removed {user} as a moderator.")
        if name == "vip":
            await helix.add_vip(b, uid)
            return HelixResult(f"You added {user} as a VIP.")
        await helix.remove_vip(b, uid)
        return HelixResult(f"You removed {user} as a VIP.")
    if name in ("mods", "vips"):
        logins = await (helix.list_moderators(b) if name == "mods" else helix.list_vips(b))
        return HelixResult(names=sorted(logins, key=str.lower))
    if name == "raid":
        await helix.start_raid(b, await _user_id(helix, user))
        return HelixResult(f"Raiding {user}.")
    if name == "unraid":
        await helix.cancel_raid(b)
        return HelixResult("Raid cancelled.")
    if name == "shoutout":
        await helix.send_shoutout(b, m, await _user_id(helix, user))
        return HelixResult(f"You gave {user} a shoutout.")
    if name == "commercial":
        secs = cmd.seconds or DEFAULT_COMMERCIAL_SECONDS
        await helix.start_commercial(b, secs)
        return HelixResult(f"Commercial started ({secs} s).")
    if name == "marker":
        await helix.create_marker(b)
        return HelixResult("Stream marker added.")
    if name == "settitle":
        await helix.modify_channel(b, title=cmd.text)
        return HelixResult(f"Title set to {cmd.text}.")
    if name == "setgame":
        results = await helix.search_categories(cmd.text)
        if not results:
            return HelixResult(f"No Twitch category matches {cmd.text}.")
        wanted = cmd.text.casefold()
        pick = next((r for r in results if r["name"].casefold() == wanted), results[0])
        await helix.modify_channel(b, game_id=pick["id"])
        return HelixResult(f"Category set to {pick['name']}.")
    if name == "uptime":
        stream = await helix.streams(channel)
        if not stream:
            return HelixResult(f"{channel} is offline")
        return HelixResult(f"{channel} has been live for {_duration(stream['started_at'], now)}")
    raise HelixError(f"Unknown command /{name}")
