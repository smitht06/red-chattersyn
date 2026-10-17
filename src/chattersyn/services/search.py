"""Chatterino-style search engine: tokenizer + predicates over a message snapshot.

Tokenizer grammar (Chatterino):
    (negation[!-])?(name\\w+):(value "..."|[^\\s]+) | [^\\s]+?(?=$|\\s)

Semantics (verified against chatterino2 master, see docs/m4.md):
- predicates AND; comma-separated values inside one predicate OR
- `!`/`-` immediately before a predicate NAME negates it; a bare `!word`
  is NOT a negation
- `from:` display OR login, case-insensitive contains
- `in:` channel name (platform-namespaced here: `in:twitch:x` / `in:youtube:x`
  to avoid merged-feed collisions — a documented divergence)
- `badge:` ci keys with short forms mod->moderator sub->subscriber
  prime->premium, checked against the merged Twitch+YouTube badge list
- `subtier:` first char of `subscriber/<value>` when len>3 else '1',
  case-SENSITIVE contains
- `regex:` ci unanchored match against plain text ONLY; invalid -> matches
  nothing. Divergence: a match timeout (Chatterino has none and a
  pathological pattern freezes the UI thread) — a pattern that times out on
  one message stops being applied.
- `has:link` TLD/IPv4 word parse (not regex); `has:` with any other name is
  an unknown predicate -> literal substring
- `is:` any-of flag set; unknown names match nothing (negated: everything)
- unknown `name:value` -> literal substring of the whole token
- quoted values strip quotes; bare words match ci over a searchText of
  username variants + content

The engine is UI-agnostic and pure — the popup owns the snapshot.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from ..models.message import Message, MessageFlags, Platform

try:
    import regex as _regex  # timeout-capable matcher (declared dependency)
except ImportError:  # pragma: no cover
    _regex = None

_REGEX_TIMEOUT = 0.2  # seconds per message — a pattern that times out is retired

_TOKEN_RE = re.compile(
    r"""(?x)
    (?P<neg>[!-])?(?P<name>\w+):
    (?:"(?P<quoted>[^"]*)"|(?P<value>[^\s]+))
    |
    (?P<bare>[^\s]+)
    """
)

# badge: short forms (Chatterino)
_BADGE_ALIASES = {"mod": "moderator", "sub": "subscriber", "prime": "premium"}

# has:link — root-zone-flavoured TLD set (minor mismatches accepted)
_TLDS = frozenset(["com", "net", "org", "edu", "gov", "mil", "int", "io", "co", "me", "tv", "gg", "cc", "ai", "ly", "im", "live", "chat", "dev", "app", "xyz", "info", "biz", "club", "online", "site", "store", "shop", "tech", "blog", "news", "media", "email", "link", "click", "top", "win", "pro", "plus", "work", "space", "agency", "digital", "us", "uk", "ca", "au", "nz", "de", "fr", "es", "it", "nl", "be", "ch", "at", "se", "no", "dk", "fi", "pl", "pt", "ie", "ru", "ua", "cz", "sk", "hu", "ro", "gr", "tr", "il", "sa", "ae", "eg", "za", "br", "mx", "ar", "cl", "co", "pe", "kr", "jp", "cn", "tw", "hk", "sg", "in", "id", "vn", "th", "ph", "my", "pk"])
_IPV4_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_URLISH_WORD_RE = re.compile(r"[^\s<>\"')\]]+")
_BADGE_SET_RE = re.compile(r"^subscriber/(.*)$")

_IS_CHECKS: dict[str, tuple[MessageFlags, ...] | None] = {
    # name -> flags (any) or None (matches nothing)
    "deleted": (MessageFlags.DELETED,),
    "disabled": (MessageFlags.DELETED,),
    "sub": (MessageFlags.SUBSCRIPTION,),
    "subscription": (MessageFlags.SUBSCRIPTION,),
    "highlighted": (MessageFlags.HIGHLIGHTED,),
    "system": (MessageFlags.SYSTEM,),
    "first-msg": (MessageFlags.FIRST_MESSAGE,),
    "cheer-msg": (MessageFlags.CHEER,),
    "reply": (MessageFlags.REPLY,),
    "announcement": (MessageFlags.ANNOUNCEMENT,),
    "membership": (MessageFlags.MEMBERSHIP,),
    "sponsor": (MessageFlags.MEMBERSHIP,),   # closest YouTube mapping
    "superchat": (MessageFlags.SUPERCHAT,),
    "super-chat": (MessageFlags.SUPERCHAT,),
    "sticker": (MessageFlags.SUPERCHAT,),    # super stickers share the flag
    # known but unrepresentable in our model -> match nothing (parity)
    "redemption": None,
    "restricted": None,
    "monitored": None,
    "shared": None,
    "watch-streak": None,
}


@dataclass
class _Token:
    """One query token: either a bare word or a name:value predicate."""

    name: str | None          # predicate name (lowercased); None = bare
    values: list[str]
    negated: bool
    raw: str = ""


class SearchEngine:
    def search(self, messages: list[Message], query: str) -> list[Message]:
        """Run `query` over the snapshot; returns matching messages in order."""
        tokens = self.tokenize(query)
        if not tokens:
            return []
        return [msg for msg in messages if self._matches(msg, tokens)]

    def tokenize(self, query: str) -> list[_Token]:
        tokens: list[_Token] = []
        for match in _TOKEN_RE.finditer(query):
            if match.group("bare") is not None:
                # a bare !word has no colon -> NOT a negation (parity)
                tokens.append(_Token(None, [match.group("bare")], False, match.group(0)))
                continue
            negated = match.group("neg") is not None
            quoted = match.group("quoted")
            value = match.group("value")
            # quoted keeps commas literal
            values = [quoted] if quoted is not None else (value or "").split(",")
            tokens.append(_Token(
                match.group("name").lower(), values, negated, match.group(0)))
        return tokens

    def _matches(self, msg: Message, tokens: list[_Token]) -> bool:
        for token in tokens:
            matched = self._token_matches(msg, token)
            if token.negated:
                matched = not matched
            if not matched:
                return False
        return True

    def _token_matches(self, msg: Message, token: _Token) -> bool:
        if token.name is None:
            return any(v.lower() in self._search_text(msg) for v in token.values)
        if token.name == "from":
            return self._match_from(msg, token)
        if token.name == "in":
            return self._match_in(msg, token)
        if token.name == "badge":
            return self._match_badge(msg, token)
        if token.name == "subtier":
            return self._match_subtier(msg, token)
        if token.name == "regex":
            return self._match_regex(msg, token)
        if token.name == "has":
            return self._match_has(msg, token)
        if token.name == "is":
            return self._match_is(msg, token)
        # unknown name:value -> literal substring of the whole token
        return any(token.raw.lower() in self._search_text(msg) for _ in token.values)

    # -- per-predicate matchers ---------------------------------------------------

    def _search_text(self, msg: Message) -> str:
        author = msg.author
        if author is None:
            return msg.plain.lower()
        return f"{author.name} {author.login} {msg.plain}".lower()

    @staticmethod
    def _match_from(msg: Message, token: _Token) -> bool:
        author = msg.author
        if author is None:
            return False
        hay = f"{author.name} {author.login}".lower()
        return any(v.lower() in hay for v in token.values)

    @staticmethod
    def _match_in(msg: Message, token: _Token) -> bool:
        # platform namespacing (divergence): in:twitch:x / in:youtube:x
        channel = msg.channel_id.lower()
        video = str(msg.extra.get("video_id") or "").lower()
        for raw in token.values:
            value = raw.lower()
            if value.startswith("twitch:"):
                if msg.platform is Platform.TWITCH and value[7:] in channel:
                    return True
            elif value.startswith("youtube:"):
                if msg.platform is Platform.YOUTUBE and (value[8:] in channel or value[8:] in video):
                    return True
            elif value in channel or (video and value in video):
                return True
        return False

    @staticmethod
    def _match_badge(msg: Message, token: _Token) -> bool:
        if msg.author is None:
            return False
        wanted = {_BADGE_ALIASES.get(v, v) for v in token.values}
        # YouTube badges carry the role in version_id (Badge("yt", "moderator"))
        # while Twitch badges use set_id — check both.
        return any(
            badge.set_id.lower() in wanted or badge.version_id.lower() in wanted
            for badge in msg.author.badges
        )

    @staticmethod
    def _match_subtier(msg: Message, token: _Token) -> bool:
        if msg.author is None:
            return False
        tiers: list[str] = []
        for badge in msg.author.badges:
            if badge.set_id.lower() != "subscriber":
                continue
            match = _BADGE_SET_RE.match(badge.version_id or "")
            value = match.group(1) if match else (badge.version_id or "")
            tiers.append(value[0] if len(value) > 3 else "1")
        # case-SENSITIVE contains (parity)
        return any(any(v in tier for tier in tiers) for v in token.values)

    def _match_regex(self, msg: Message, token: _Token) -> bool:
        if _regex is None:
            return False
        for pattern in token.values:
            try:
                if _regex.search(pattern, msg.plain, flags=_regex.IGNORECASE,
                                 timeout=_REGEX_TIMEOUT) is not None:
                    return True
            except TimeoutError:
                # pathological pattern: retire it for this search run (divergence)
                token.values = []
                return False
            except _regex.error:
                return False  # invalid regex matches nothing
        return False

    def _match_has(self, msg: Message, token: _Token) -> bool:
        if "link" in token.values:
            return self._has_link(msg.plain)
        # unknown has: name -> literal substring
        return any(token.raw.lower() in msg.plain.lower() for _ in token.values)

    def _match_is(self, msg: Message, token: _Token) -> bool:
        for value in token.values:
            if value in ("timeout", "ban"):
                # CLEARCHAT renders as a system row; no dedicated flag exists
                if msg.is_system and (
                        "timed out" in msg.plain.lower()
                        or "banned" in msg.plain.lower()):
                    return True
                continue
            checks = _IS_CHECKS.get(value, "missing")
            if checks == "missing":
                continue  # unknown names match nothing
            if checks is None:
                continue
            if any(msg.flags & flag for flag in checks):
                return True
        return False

    @staticmethod
    def _has_link(plain: str) -> bool:
        for word in _URLISH_WORD_RE.findall(plain):
            candidate = word.strip(".,;:!?()[]{}")
            if not candidate:
                continue
            lower = candidate.lower()
            if lower.startswith(("http://", "https://", "www.")):
                return True
            if _IPV4_RE.fullmatch(candidate):
                try:
                    ipaddress.ip_address(candidate)
                    return True
                except ValueError:
                    pass
            labels = lower.rstrip(".").split(".")
            if (len(labels) >= 2 and labels[-1] in _TLDS
                    and all(labels) and all(re.fullmatch(r"[a-z0-9-]+", label) for label in labels)):
                return True
        return False
