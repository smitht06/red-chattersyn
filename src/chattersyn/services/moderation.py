"""ModerationState: per-channel mod rights and moderation bookkeeping.

Drives the tab-header shield and per-row action buttons (docs/m4.md
"Moderation mode"):

- Twitch: hasModRights = the channel's USERSTATE mod flag, own USERSTATE
  badges (moderator/lead_moderator/staff/admin/global_mod), or being the
  broadcaster (display name == channel). Identity comes from
  GLOBALUSERSTATE via ``set_self_identity``.
- YouTube: moderation (server ban + local delete) is allowed when the
  connected account owns the stream's channel (``mine=true`` identity) —
  while ownership is unknown the shield stays hidden.
- ClearChat bookkeeping: timed-out/banned users are registered so their
  rows can be grayed (MessageFlags.DISABLED), and repeated timeouts on the
  same user within a short window fold into "(N times)".
- YouTube bans: liveChatBans.insert returns a ban id per (live_chat_id,
  channel_id); it is remembered here so Unban can delete it.

The class is pure bookkeeping (no Qt, no I/O); MainWindow feeds it from
bridge events and reads it back when rendering rows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Badge sets that carry moderator powers anywhere (own USERSTATE badges).
# docs/m4.md: moderator/lead_moderator/mod=1; staff/admin/global_mod hold
# platform-wide powers. twitchbot is a bot badge, not a mod right.
MOD_BADGE_SETS = {"moderator", "lead_moderator", "staff", "admin", "global_mod"}

# Fold repeated ClearChat rows for the same user within this window.
STACK_WINDOW = 5.0  # seconds
STACK_MAX_MESSAGES = 20  # messages since the previous row


@dataclass
class _DisabledUser:
    login: str
    until: float | None = None  # monotonic deadline; None = permanent
    stacked: list[float] = field(default_factory=list)  # ClearChat times in window


class ModerationState:
    def __init__(self) -> None:
        self._display_name: str | None = None
        self._twitch_mod: set[str] = set()  # channels with USERSTATE mod=1
        self._twitch_badge_mod: set[str] = set()  # channels with own mod badges
        self._youtube_owner: dict[str, bool] = {}  # live_chat_id -> is mine
        self._disabled: dict[str, dict[str, _DisabledUser]] = {}  # channel -> login
        self._youtube_bans: dict[tuple[str, str], str] = {}  # (live_chat_id, channel_id) -> ban id

    # -- identity ---------------------------------------------------------------

    def set_self_identity(self, display_name: str | None) -> None:
        """The connected account's display name (GLOBALUSERSTATE)."""
        self._display_name = display_name

    # -- Twitch ------------------------------------------------------------------

    def set_twitch_mod(self, channel: str, mod: bool) -> None:
        """USERSTATE mod flag for one joined channel."""
        channel = channel.lstrip("#").lower()
        if mod:
            self._twitch_mod.add(channel)
        else:
            self._twitch_mod.discard(channel)

    def note_twitch_self_badges(self, channel: str, badges: list) -> None:
        """Own USERSTATE/PRIVMSG badges — moderator-class sets grant rights."""
        channel = channel.lstrip("#").lower()
        if any(getattr(b, "set_id", "") in MOD_BADGE_SETS for b in badges):
            self._twitch_badge_mod.add(channel)
        else:
            self._twitch_badge_mod.discard(channel)

    def has_twitch_rights(self, channel: str) -> bool:
        """Mod rights in a Twitch channel (broadcaster or moderator)."""
        channel = channel.lstrip("#").lower()
        if self._display_name and self._display_name.lower() == channel:
            return True
        return channel in self._twitch_mod or channel in self._twitch_badge_mod

    def note_clearchat(self, channel: str, login: str, duration: int | None) -> int:
        """Register a timeout/ban. Returns the stacked count (1 = first).

        Repeated ClearChats for the same user within STACK_WINDOW seconds
        fold onto the previous row ("(N times)"); MainWindow also prunes
        stacks older than the window.
        """
        channel = channel.lstrip("#").lower()
        login = login.lower()
        users = self._disabled.setdefault(channel, {})
        entry = users.get(login)
        now = time.monotonic()
        # None = permanent; 0 = expired already (unban ClearChats never
        # register here — MainWindow routes those through clear_disabled).
        deadline = now + duration if duration is not None else None
        if entry is None:
            entry = _DisabledUser(login=login, until=deadline, stacked=[now])
            users[login] = entry
        else:
            entry.until = deadline
            entry.stacked.append(now)
        return len(entry.stacked)

    def last_clearchat(self, channel: str, login: str) -> float | None:
        """Monotonic time of the most recent ClearChat for (channel, login),
        or None when no stack is active."""
        channel = channel.lstrip("#").lower()
        entry = (self._disabled.get(channel) or {}).get(login.lower())
        if entry is None or not entry.stacked:
            return None
        return entry.stacked[-1]

    def clear_stack(self, channel: str, login: str) -> int:
        """Forget the stack (a new first row is being rendered). Returns 0."""
        channel = channel.lstrip("#").lower()
        entry = (self._disabled.get(channel) or {}).get(login.lower())
        if entry is not None:
            entry.stacked.clear()
        return 0

    def clear_disabled(self, channel: str, login: str) -> None:
        """Forget a timeout/ban entry (untimeout / ban-duration=0 ClearChat)."""
        channel = channel.lstrip("#").lower()
        users = self._disabled.get(channel)
        if users is not None:
            users.pop(login.lower(), None)

    def clear_stacks_for_channel(self, channel: str) -> None:
        """A full CLEARCHAT breaks every "(N times)" stack in the channel —
        a later timeout for the same user starts a fresh row below the clear
        marker instead of folding onto the pre-clear one. Active timeouts
        and bans stay registered."""
        channel = channel.lstrip("#").lower()
        for entry in (self._disabled.get(channel) or {}).values():
            entry.stacked.clear()

    def disabled_until(self, channel: str, login: str) -> float | None:
        """The stored expiry deadline (monotonic) for a disabled user, or
        None. A pure accessor: expiry pruning happens in disabled_logins."""
        channel = channel.lstrip("#").lower()
        entry = (self._disabled.get(channel) or {}).get(login.lower())
        return entry.until if entry is not None else None

    def disabled_logins(self, channel: str) -> set[str]:
        """Users currently timed out/banned in `channel` (rows to gray)."""
        channel = channel.lstrip("#").lower()
        now = time.monotonic()
        users = self._disabled.get(channel, {})
        expired = [login for login, e in users.items()
                   if e.until is not None and e.until < now]
        for login in expired:
            users.pop(login, None)
        return set(users)

    def is_disabled(self, channel: str, login: str) -> bool:
        return login.lower() in self.disabled_logins(channel)

    # -- YouTube ------------------------------------------------------------------

    def set_youtube_owner(self, live_chat_id: str, mine: bool) -> None:
        self._youtube_owner[live_chat_id] = mine

    def clear_youtube_ownership(self) -> None:
        """Drop every ownership entry (the connected account changed)."""
        self._youtube_owner.clear()

    def forget_youtube_chat(self, live_chat_id: str) -> None:
        """A stream's chat ended: drop its owner entry and ban-id memory."""
        self._youtube_owner.pop(live_chat_id, None)
        for key in [key for key in self._youtube_bans if key[0] == live_chat_id]:
            self._youtube_bans.pop(key, None)

    def youtube_owner_known(self, live_chat_id: str) -> bool:
        return live_chat_id in self._youtube_owner

    def has_youtube_rights(self, live_chat_id: str) -> bool:
        return self._youtube_owner.get(live_chat_id, False)

    def remember_youtube_ban(self, live_chat_id: str, channel_id: str, ban_id: str) -> None:
        self._youtube_bans[(live_chat_id, channel_id)] = ban_id

    def youtube_ban_id(self, live_chat_id: str, channel_id: str) -> str | None:
        return self._youtube_bans.get((live_chat_id, channel_id))

    def clear_youtube_ban(self, live_chat_id: str, channel_id: str) -> None:
        self._youtube_bans.pop((live_chat_id, channel_id), None)
