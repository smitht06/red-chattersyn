"""Chatterino-style command system: builtins + commands.json user commands.

Dispatch order (mirrors Chatterino's CommandController::execCommand):
  1. (emoji shortcode replacement — lands with the emote work, step 8)
  2. split on spaces
  3. if the first word(s) match a USER command trigger, substitute its func
     and re-run the whole pipeline (custom commands can emit builtins)
  4. match builtins by first word (skipped when dry_run=True)
  5. multi-word user triggers are matched in step 3 already
  6. otherwise: unknown '/'- or '.'-prefixed input is blocked with a system
     row (Chatterino's unknown-command send guard), EXCEPT /me and .me which
     pass through to Twitch (the server renders the ACTION).

The engine is UI-agnostic: dispatch() returns a CommandOutcome and the UI
layer (MainWindow) executes it — callbacks in CommandContext reach into the
tab's message model / clipboard / browser.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models.message import Message, Platform

log = logging.getLogger(__name__)

_RECURSION_LIMIT = 5

# Inputs passed through to Twitch (the server handles them): /me renders the
# ACTION, /help /disconnect /host /unhost are answered by a server NOTICE.
_PASSTHROUGH_COMMANDS = ("me", "help", "disconnect", "host", "unhost")

# Placeholder grammar, exact Chatterino regex:
# {N} = word N after the trigger; {N+} = words N..end; {var;fallback} used
# when the value is empty; {{1} escapes to literal {1}; unknown names stay
# literal; out-of-range {N} produces the empty string.
_PLACEHOLDER_RE = re.compile(r"(^|[^{])({{)*{(\d+\+?|([a-zA-Z.-]+)(?:;(.+?))?)}")

# Brace escape: a run of 2+ braces directly before a placeholder strips one
# brace and protects the placeholder from expansion. The exact Chatterino
# regex cannot match "{{1}" (the [^{] guard rejects the preceding brace), so
# the escape is applied in a separate pass with a sentinel for the opener.
_ESCAPED_RE = re.compile(r"({+)({\d+\+?}|{[a-zA-Z.-]+(?:;[^}]*)?})")
_ESCAPE_SENTINEL = "\x00"

# Moderation/broadcaster chat commands sent verbatim over PRIVMSG: the server
# acts and answers with a NOTICE (msg-id feedback mapped in the IRC mapper).
# Everything here is a documented Twitch IRC chat command (docs/m4.md).
_IRC_MOD_COMMANDS = (
    "ban", "banid", "timeout", "untimeout", "unban", "warn",
    "mod", "unmod", "vip", "unvip",
    "announce", "announceblue", "announcegreen", "announceorange", "announcepurple",
    "slow", "slowoff", "followers", "followersoff",
    "emoteonly", "emoteonlyoff", "subscribers", "subscribersoff",
    "uniquechat", "r9kbeta", "uniquechatoff", "r9kbetaoff",
    "raid", "unraid", "shoutout", "commercial", "shield", "shieldoff",
    "marker", "clear",
)

# Helix-only / client-side-feature commands — honest stubs until later work
# (/delete is deliberately deferred: Helix Delete Chat Messages needs the
# channel:moderate scope; see docs/m4.md decisions).
_MOD_STUB_COMMANDS = (
    "delete", "monitor", "restrict", "unmonitor", "unrestrict", "mods", "vips",
    "clip", "poll", "cancelpoll", "endpoll",
    "prediction", "lockprediction", "cancelprediction", "completeprediction",
    "pin", "unpin", "settitle", "setgame", "uptime", "streamlink",
    "requests", "lowtrust", "raw",
)

# (min_args, max_args) per IRC mod command; max None = unbounded.
# docs/m4.md arg syntax: /ban <user> [reason], /timeout <user> [dur][unit]
# [reason], /warn <user> <reason>, /commercial <30|60|90|120|150|180>.
_IRC_MOD_SIG: dict[str, tuple[int, int | None]] = {
    # ban/timeout take an optional free-text reason tail (Twitch joins the
    # remaining words; Chatterino sends them through) — unbounded max.
    # /warn REQUIRES the reason (docs/m4.md), so it starts at 2.
    "ban": (1, None), "banid": (1, 1), "unban": (1, 1), "untimeout": (1, 1),
    "timeout": (1, None), "warn": (2, None), "mod": (1, 1), "unmod": (1, 1),
    "vip": (1, 1), "unvip": (1, 1), "shoutout": (1, 1), "raid": (1, 1),
    "unraid": (0, 0), "clear": (0, 0), "slow": (0, 1), "slowoff": (0, 0),
    "followers": (0, 1), "followersoff": (0, 0),
    "emoteonly": (0, 0), "emoteonlyoff": (0, 0),
    "subscribers": (0, 0), "subscribersoff": (0, 0),
    "uniquechat": (0, 0), "uniquechatoff": (0, 0),
    "r9kbeta": (0, 0), "r9kbetaoff": (0, 0),
    "shield": (0, 0), "shieldoff": (0, 0),
    "commercial": (0, 1), "marker": (0, 0),
    "announce": (1, None), "announceblue": (1, None), "announcegreen": (1, None),
    "announceorange": (1, None), "announcepurple": (1, None),
}

_IRC_MOD_USAGE = {
    "ban": "Usage: /ban <username> [reason]",
    "banid": "Usage: /banid <user-id>",
    "timeout": "Usage: /timeout <username> [duration, e.g. 300 / 10m / 1d] [reason] (default 10m)",
    "warn": "Usage: /warn <username> <reason>",
    "unban": "Usage: /unban <username>",
    "untimeout": "Usage: /untimeout <username>",
    "mod": "Usage: /mod <username>",
    "unmod": "Usage: /unmod <username>",
    "vip": "Usage: /vip <username>",
    "unvip": "Usage: /unvip <username>",
    "shoutout": "Usage: /shoutout <channel>",
    "raid": "Usage: /raid <channel>",
    "announce": "Usage: /announce <message>",
    "announceblue": "Usage: /announceblue <message>",
    "announcegreen": "Usage: /announcegreen <message>",
    "announceorange": "Usage: /announceorange <message>",
    "announcepurple": "Usage: /announcepurple <message>",
    "slow": "Usage: /slow [seconds]",
}

# IRC mod commands whose argument is a username (strip a leading @).
_IRC_MOD_USER_ARGS = {"ban", "unban", "timeout", "untimeout", "warn",
                      "mod", "unmod", "vip", "unvip", "shoutout", "raid"}

# 300 / 300s / 10m / 1d2h / 1w2d3h4m5s — combos are documented (docs/m4.md).
# ASCII digits only (Twitch rejects Unicode digits) and every number in a
# combo carries a unit — a trailing bare number like '2w3' is malformed,
# not a silent extra.
_DURATION_RE = re.compile(r"^(?:[0-9]+[smhdw])+$")
_DURATION_PART = re.compile(r"([0-9]+)([smhdw])")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
TIMEOUT_MAX_SECONDS = 1209600  # Twitch's 2-week cap (docs/m4.md)


def _duration_seconds(text: str) -> int:
    """'1d2h' -> 93600; bare digits ('600') count as seconds. Callers have
    already validated the shape; -1 signals a value too large to convert
    (Python's int() digit cap) — the caller's range check rejects it."""
    try:
        if _DURATION_PART.search(text):
            return sum(int(value) * _DURATION_SECONDS[unit]
                       for value, unit in _DURATION_PART.findall(text))
        return int(text)
    except ValueError:
        return -1


@dataclass
class UserCommand:
    name: str
    func: str
    show_in_context_menu: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> UserCommand | None:
        name = str(data.get("name") or "").strip()
        func = str(data.get("func") or "")
        if not name or not func:
            return None
        return cls(name=name, func=func, show_in_context_menu=bool(data.get("showInMsgContextMenu")))

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "func": self.func,
                "showInMsgContextMenu": self.show_in_context_menu}


def load_user_commands(config_dir_path: Path) -> list[UserCommand]:
    """Load commands.json (Chatterino format); unreadable file -> empty."""
    path = config_dir_path / "commands.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        log.warning("Could not read %s: %s — no user commands loaded", path, exc)
        return []
    commands = data.get("commands", []) if isinstance(data, dict) else []
    if not isinstance(commands, list):
        return []
    return [cmd for entry in commands if isinstance(entry, dict)
            if (cmd := UserCommand.from_json(entry)) is not None]


def save_user_commands(config_dir_path: Path, commands: list[UserCommand]) -> None:
    path = config_dir_path / "commands.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(
        {"commands": [cmd.to_json() for cmd in commands]}, indent=2), encoding="utf-8")
    tmp.replace(path)


def expand_placeholders(template: str, words: list[str], variables: dict[str, str]) -> str:
    """Chatterino placeholder semantics ({1}, {2+}, {var;fallback}, {{1} escape)."""
    template = _ESCAPED_RE.sub(
        lambda m: m.group(1)[1:] + _ESCAPE_SENTINEL + m.group(2)[1:], template)
    out: list[str] = []
    pos = 0
    for match in _PLACEHOLDER_RE.finditer(template):
        prefix, braces, inner = match.group(1), match.group(2) or "", match.group(3)
        start = match.start()
        out.append(template[pos:start])
        out.append(prefix)  # the (^|[^{]) char is part of the match, not the value
        if braces:
            # {{...} strips one brace level and emits the placeholder literally.
            out.append("{" * (len(braces) // 2 - 1))
            out.append("{" + inner + "}")
        else:
            value = _value_of(inner, words, variables)
            if value is None:  # unknown variable name: leave it literally
                out.append("{" + inner + "}")
            else:
                out.append(value)
        pos = match.end()
    out.append(template[pos:])
    return "".join(out).replace(_ESCAPE_SENTINEL, "{")


def _value_of(inner: str, words: list[str], variables: dict[str, str]) -> str | None:
    if inner.isdigit():
        idx = int(inner)
        return words[idx - 1] if 0 < idx <= len(words) else ""
    if inner.endswith("+") and inner[:-1].isdigit():
        idx = int(inner[:-1])
        return " ".join(words[idx - 1:]) if 0 < idx <= len(words) else ""
    name, _, fallback = inner.partition(";")
    if name in variables:
        value = variables[name]
        return value if value else fallback
    return None  # unknown name -> literal


@dataclass
class CommandContext:
    """Per-dispatch context bound to one tab (callbacks set by the UI layer)."""

    twitch_channel: str = ""
    youtube_live_chat_id: str = ""
    my_login: str = ""
    my_name: str = ""
    find_last_from: Callable[[str], Message | None] | None = None
    clear_view: Callable[[], None] | None = None
    copy_text: Callable[[str], None] | None = None
    open_url: Callable[[str], None] | None = None
    open_mentions: Callable[[], None] | None = None
    list_chatters: Callable[[], list[str]] | None = None
    open_usercard: Callable[[str], None] | None = None
    # Extra placeholder variables for context-menu runs (moderation actions:
    # user.name / user.login / msg.id / msg.text) — merged over the defaults.
    extra_variables: dict[str, str] = field(default_factory=dict)


@dataclass
class CommandOutcome:
    """What the UI should do after dispatch."""

    handled: bool
    system: str = ""                        # system row to show
    system_platform: Platform = Platform.TWITCH
    send_text: str = ""                     # send through the normal dual path
    twitch_only: bool = False               # disable the YouTube toggle for this send
    reply_parent_id: str = ""
    reply_parent_login: str = ""


class CommandController:
    def __init__(self, settings, user_commands: list[UserCommand] | None = None):
        self._settings = settings
        self._user_commands = user_commands or []
        self._builtins: dict[str, Callable[[CommandContext, list[str], str], CommandOutcome]] = {}
        self._register_builtins()

    @property
    def user_commands(self) -> list[UserCommand]:
        return self._user_commands

    def set_user_commands(self, commands: list[UserCommand]) -> None:
        self._user_commands = commands

    def builtin_names(self) -> list[str]:
        """Registered '/'-prefixed names (for autocomplete)."""
        return [f"/{name}" for name in self._builtins]

    # -- dispatch ---------------------------------------------------------------------

    def dispatch(self, text: str, ctx: CommandContext, *, dry_run: bool = False) -> CommandOutcome:
        return self._dispatch(text, ctx, dry_run=dry_run, depth=0)

    def _dispatch(self, text: str, ctx: CommandContext, *, dry_run: bool, depth: int) -> CommandOutcome:
        if depth > _RECURSION_LIMIT:
            return CommandOutcome(True, system="Command recursion limit reached.")
        words = text.split()
        if not words:
            return CommandOutcome(False)
        first = words[0]
        user = self._match_user(words)
        if user is not None:
            # User triggers shadow builtins; substitution re-runs the pipeline.
            rest = words[len(user.name.split()):]
            expanded = expand_placeholders(user.func, rest, self._variables(ctx))
            if dry_run:
                return CommandOutcome(True, send_text=expanded)  # preview only
            if expanded[:1] in ("/", "."):
                return self._dispatch(expanded, ctx, dry_run=dry_run, depth=depth + 1)
            return CommandOutcome(True, send_text=expanded)
        name = first[1:] if first[:1] in ("/", ".") else ""
        if not name:
            return CommandOutcome(False)
        if name.lower() in _PASSTHROUGH_COMMANDS:
            return CommandOutcome(False)  # pass-through: Twitch/server handles it
        handler = self._builtins.get(name.lower())
        if handler is None:
            return CommandOutcome(True, system=f"Unknown command: {first}")
        if dry_run:
            return CommandOutcome(True)  # recognized but not executed (parity)
        return handler(ctx, words[1:], text)

    def _match_user(self, words: list[str]) -> UserCommand | None:
        # Chatterino: the trigger name has no prefix, users type "/name …";
        # accept both the bare name and a name that includes its own prefix.
        first = words[0]
        for command in self._user_commands:
            trigger = command.name.split()
            if not trigger:
                continue
            if trigger[0] not in (first, first.lstrip("/.")):
                continue
            if words[1:len(trigger)] == trigger[1:]:
                return command
        return None

    def _variables(self, ctx: CommandContext) -> dict[str, str]:
        # msg.id/msg.text/user.name resolve only in context-menu runs
        # (moderation actions) via extra_variables — absent for typed
        # commands, as in Chatterino.
        variables = {
            "channel.name": ctx.twitch_channel,
            "channel": ctx.twitch_channel,  # deprecated alias
            "channel.id": ctx.twitch_channel,  # room id lands with step 9
            "my.name": ctx.my_name or ctx.my_login,
            "my.id": ctx.my_login,
            "input.text": "",
            "element.copytext": "",
        }
        variables.update(ctx.extra_variables)
        return variables

    # -- builtins ------------------------------------------------------------------------

    def _register_builtins(self) -> None:
        self._builtins.update({
            "reply": self._cmd_reply,
            "clearmessages": self._cmd_clearmessages,
            "copy": self._cmd_copy,
            "openurl": self._cmd_openurl,
            "popout": self._cmd_popout,
            "popup": self._cmd_popout,
            "user": self._cmd_user,
            "mentions": self._cmd_mentions,
            "usercard": self._cmd_usercard,
            "chatters": self._cmd_chatters,
        })
        for name in _IRC_MOD_COMMANDS:
            self._builtins[name] = self._cmd_irc_mod
        for name in _MOD_STUB_COMMANDS:
            self._builtins[name] = self._cmd_stub

    def _cmd_reply(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        if not ctx.twitch_channel:
            return self._twitch_only("reply")
        if len(args) < 2:
            return CommandOutcome(True, system="Usage: /reply <username> <message>")
        login = args[0].lstrip("@").lower()
        parent = ctx.find_last_from(login) if ctx.find_last_from is not None else None
        if parent is None or parent.author is None:
            return CommandOutcome(True, system="A message from that user wasn't found.")
        return CommandOutcome(
            True, send_text=" ".join(args[1:]),
            reply_parent_id=parent.id,
            reply_parent_login=parent.author.login or login,
        )

    def _cmd_clearmessages(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        # Local, visual clear only — explicitly NOT the /clear moderator command.
        if ctx.clear_view is not None:
            ctx.clear_view()
        return CommandOutcome(True)

    def _cmd_irc_mod(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        """One of the server-side moderation commands: validate the user's
        arguments, then send the literal command over PRIVMSG (Twitch-only) —
        the server's NOTICE feedback comes back as a mapped system row."""
        # Lowercase: the registry lookup lowercases, so "/Ban" must behave
        # exactly like "/ban" (all _IRC_MOD_* tables are keyed lowercase).
        name = raw.split()[0].lstrip("/.").lower()
        if not ctx.twitch_channel:
            return self._twitch_only(name)
        lo, hi = _IRC_MOD_SIG[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            return CommandOutcome(True, system=_IRC_MOD_USAGE.get(
                name, f"Usage: /{name} <username>"))
        out_args = list(args)
        if name in _IRC_MOD_USER_ARGS:
            out_args[0] = out_args[0].lstrip("@")
        if name == "timeout" and len(out_args) == 1:
            # Chatterino sends 600 when no duration is given (docs/m4.md).
            out_args.append("600")
        if name == "timeout" and len(out_args) >= 2:
            duration = out_args[1]
            if not (_DURATION_RE.fullmatch(duration)
                    or (duration.isascii() and duration.isdigit())):
                return CommandOutcome(True, system=f"Invalid timeout duration: {duration}")
            seconds = _duration_seconds(duration)
            if not 1 <= seconds <= TIMEOUT_MAX_SECONDS:
                return CommandOutcome(
                    True, system=f"Invalid timeout duration: {duration} (1s – 2w)")
        return CommandOutcome(
            True, send_text=f"/{name} {' '.join(out_args)}".strip(), twitch_only=True)

    def _cmd_usercard(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        user = args[0].lstrip("@") if args else ""
        if not user:
            return CommandOutcome(True, system="Usage: /usercard <username>")
        if ctx.open_usercard is not None:
            ctx.open_usercard(user)
        return CommandOutcome(True)

    def _cmd_chatters(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        if ctx.list_chatters is None:
            return CommandOutcome(True, system="The chatter list is not available here.")
        names = ctx.list_chatters()
        if not names:
            return CommandOutcome(True, system="No chatters recorded yet.")
        shown = ", ".join(names[:50])
        more = f" (+{len(names) - 50} more)" if len(names) > 50 else ""
        return CommandOutcome(True, system=f"Chatters ({len(names)}): {shown}{more}")

    def _cmd_copy(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        if ctx.copy_text is not None:
            ctx.copy_text(" ".join(args))
        return CommandOutcome(True)

    def _cmd_openurl(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        url = next((arg for arg in args if not arg.startswith("--")), "")
        if url.startswith(("http://", "https://")):
            if ctx.open_url is not None:
                ctx.open_url(url)
            return CommandOutcome(True)
        return CommandOutcome(True, system="Usage: /openurl <http(s):// URL>")

    def _cmd_popout(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        channel = args[0].lstrip("#@") if args else ctx.twitch_channel
        if ctx.open_url is not None:
            ctx.open_url(f"https://www.twitch.tv/popout/{channel}/chat")
        return CommandOutcome(True)

    def _cmd_user(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        user = args[0].lstrip("@") if args else ""
        if not user:
            return CommandOutcome(True, system="Usage: /user <username>")
        if ctx.open_url is not None:
            ctx.open_url(f"https://www.twitch.tv/{user}")
        return CommandOutcome(True)

    def _cmd_mentions(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        if ctx.open_mentions is not None:
            ctx.open_mentions()
        return CommandOutcome(True)

    def _cmd_stub(self, ctx: CommandContext, args: list[str], raw: str) -> CommandOutcome:
        name = raw.split()[0].lstrip("/.").lower()
        return CommandOutcome(True, system=f"The /{name} command is not implemented yet.")

    def _twitch_only(self, name: str) -> CommandOutcome:
        return CommandOutcome(True, system=f"The /{name} command only works in Twitch channels.")
