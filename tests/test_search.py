"""Search engine: tokenizer, predicates, negation, AND/OR semantics."""

import datetime as dt

import pytest

from chattersyn.models.message import Author, Badge, Message, MessageFlags, Platform
from chattersyn.services.search import SearchEngine

ENGINE = SearchEngine()


def _msg(text="hello world", platform=Platform.TWITCH, channel="forsen",
         name="Alice", login="alice", flags=MessageFlags.NONE,
         badges=None, msg_id="m1", extra=None, author=None, version="1") -> Message:
    if author is None and name is not None:
        author = Author(name=name, login=login,
                        badges=badges if badges is not None else [
                            Badge("subscriber", version, "Subscriber")])
    return Message(
        id=msg_id, platform=platform, channel_id=channel,
        ts=dt.datetime(2026, 10, 16, 9, 5, 7),
        author=author, plain=text, flags=flags, extra=extra or {},
    )


# -- tokenizer ------------------------------------------------------------------------


def test_tokenize_bare_words():
    tokens = ENGINE.tokenize("hello world")
    assert [(t.name, t.values) for t in tokens] == [(None, ["hello"]), (None, ["world"])]


def test_tokenize_predicate_and_quotes():
    tokens = ENGINE.tokenize('from:"alice smith" badge:mod')
    assert tokens[0].name == "from"
    assert tokens[0].values == ["alice smith"]
    assert tokens[1].values == ["mod"]


def test_tokenize_comma_values_are_or():
    tokens = ENGINE.tokenize("is:deleted,ban")
    assert tokens[0].values == ["deleted", "ban"]


def test_tokenize_negation():
    tokens = ENGINE.tokenize("!from:alice -badge:mod")
    assert tokens[0].negated and tokens[1].negated


def test_tokenize_bare_exclamation_is_not_negated():
    tokens = ENGINE.tokenize("!word")
    assert tokens[0].name is None
    assert not tokens[0].negated
    assert tokens[0].values == ["!word"]


def test_tokenize_quoted_value_keeps_comma():
    tokens = ENGINE.tokenize('from:"a,b"')
    assert tokens[0].values == ["a,b"]


# -- bare words + from: -----------------------------------------------------------------


def test_bare_word_matches_name_login_and_content():
    assert ENGINE._matches(_msg(text="Kappa", name="xXaliceXx"), ENGINE.tokenize("xXaliceXx"))
    assert ENGINE._matches(_msg(), ENGINE.tokenize("HELLO"))  # ci
    assert not ENGINE._matches(_msg(), ENGINE.tokenize("absent"))


def test_from_matches_display_or_login():
    msg = _msg(name="Alice Wonder", login="alicew")
    assert ENGINE._matches(msg, ENGINE.tokenize("from:alicew"))
    assert ENGINE._matches(msg, ENGINE.tokenize("from:WONDER"))
    assert not ENGINE._matches(msg, ENGINE.tokenize("from:bob"))


def test_from_negated():
    assert not ENGINE._matches(_msg(), ENGINE.tokenize("!from:alice"))


# -- in: --------------------------------------------------------------------------------


def test_in_matches_channel_case_insensitive():
    assert ENGINE._matches(_msg(channel="Forsen"), ENGINE.tokenize("in:forsen"))
    assert not ENGINE._matches(_msg(channel="xqc"), ENGINE.tokenize("in:forsen"))


def test_in_platform_namespaced():
    tw = _msg(channel="forsen", platform=Platform.TWITCH)
    yt = _msg(channel="Cg0K", platform=Platform.YOUTUBE, extra={"video_id": "vid123"})
    assert ENGINE._matches(tw, ENGINE.tokenize("in:twitch:forsen"))
    assert not ENGINE._matches(yt, ENGINE.tokenize("in:twitch:forsen"))
    assert ENGINE._matches(yt, ENGINE.tokenize("in:youtube:vid123"))
    assert not ENGINE._matches(tw, ENGINE.tokenize("in:youtube:vid123"))
    # un-namespaced matches either platform
    assert ENGINE._matches(yt, ENGINE.tokenize("in:Cg0K"))


# -- badge: / subtier: --------------------------------------------------------------------


def test_badge_alias_and_exact():
    msg = _msg(badges=[Badge("moderator", "1", "Moderator")])
    assert ENGINE._matches(msg, ENGINE.tokenize("badge:mod"))
    assert ENGINE._matches(msg, ENGINE.tokenize("badge:moderator"))
    assert not ENGINE._matches(msg, ENGINE.tokenize("badge:premium"))


def test_badge_checks_merged_list():
    yt = _msg(platform=Platform.YOUTUBE, badges=[Badge("yt", "moderator", "Moderator")])
    assert ENGINE._matches(yt, ENGINE.tokenize("badge:moderator"))


def test_subtier_first_char_of_version():
    assert ENGINE._matches(_msg(version="302569465"), ENGINE.tokenize("subtier:3"))
    assert not ENGINE._matches(_msg(version="302569465"), ENGINE.tokenize("subtier:1"))
    assert ENGINE._matches(_msg(version="1"), ENGINE.tokenize("subtier:1"))
    # case-SENSITIVE: "3" matches, but no uppercase variant exists to invert
    assert not ENGINE._matches(_msg(version="1"), ENGINE.tokenize("subtier:3"))


def test_subtier_without_badge():
    msg = _msg(badges=[])
    assert not ENGINE._matches(msg, ENGINE.tokenize("subtier:3"))


# -- regex: -------------------------------------------------------------------------------


def test_regex_ci_unanchored():
    assert ENGINE._matches(_msg("hello WORLD"), ENGINE.tokenize("regex:world"))
    assert ENGINE._matches(_msg("hello world"), ENGINE.tokenize("regex:^hello"))
    assert not ENGINE._matches(_msg("hello"), ENGINE.tokenize("regex:^world"))


def test_regex_invalid_matches_nothing():
    assert not ENGINE._matches(_msg(), ENGINE.tokenize("regex:([unclosed"))
    # negated invalid matches everything (uniform negation semantics)
    assert ENGINE._matches(_msg(), ENGINE.tokenize("!regex:([unclosed"))


def test_regex_timeout_retires_pattern(monkeypatch):
    from chattersyn.services import search as search_mod

    monkeypatch.setattr(search_mod, "_REGEX_TIMEOUT", 0.001)
    msg = _msg(text="a" * 300)
    # (a+)+b is catastrophic on a run of a's with no b
    assert not ENGINE._matches(msg, ENGINE.tokenize("regex:(a+)+b"))


def test_regex_plain_text_only():
    msg = _msg(text="hello", name="regexword")
    assert not ENGINE._matches(msg, ENGINE.tokenize("regex:^regexword"))


# -- has:link -----------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "check https://example.com/x out",
    "go to www.example.com",
    "ip 192.168.0.1 here",
    "bare google.com word",
    "trailing dot google.com.",
])
def test_has_link_true(text):
    assert ENGINE._matches(_msg(text), ENGINE.tokenize("has:link"))


@pytest.mark.parametrize("text", [
    "no link here",
    "version 1.2.3 is old",
    "file.txt is not a link",
    "one.two.three.four",
])
def test_has_link_false(text):
    assert not ENGINE._matches(_msg(text), ENGINE.tokenize("has:link"))


# -- is: -----------------------------------------------------------------------------------


@pytest.mark.parametrize("value,flags,expected", [
    ("deleted", MessageFlags.DELETED, True),
    ("disabled", MessageFlags.DELETED, True),
    ("sub", MessageFlags.SUBSCRIPTION, True),
    ("highlighted", MessageFlags.HIGHLIGHTED, True),
    ("first-msg", MessageFlags.FIRST_MESSAGE, True),
    ("cheer-msg", MessageFlags.CHEER, True),
    ("reply", MessageFlags.REPLY, True),
    ("announcement", MessageFlags.ANNOUNCEMENT, True),
    ("membership", MessageFlags.MEMBERSHIP, True),
    ("superchat", MessageFlags.SUPERCHAT, True),
    ("sticker", MessageFlags.SUPERCHAT, True),
    ("deleted", MessageFlags.NONE, False),
])
def test_is_flags(value, flags, expected):
    assert ENGINE._matches(_msg(flags=flags), ENGINE.tokenize(f"is:{value}")) is expected


def test_is_system_and_timeout():
    sys_msg = _msg(text="alice has been timed out for 10 minutes",
                   author=None, flags=MessageFlags.SYSTEM, badges=[])
    assert ENGINE._matches(sys_msg, ENGINE.tokenize("is:system"))
    assert ENGINE._matches(sys_msg, ENGINE.tokenize("is:timeout"))
    ban_msg = _msg(text="bob has been permanently banned",
                   author=None, flags=MessageFlags.SYSTEM, badges=[])
    assert ENGINE._matches(ban_msg, ENGINE.tokenize("is:ban"))
    # a regular message is not a timeout row
    assert not ENGINE._matches(_msg(), ENGINE.tokenize("is:timeout"))


def test_is_unknown_matches_nothing_negated_everything():
    assert not ENGINE._matches(_msg(), ENGINE.tokenize("is:nonsense"))
    assert ENGINE._matches(_msg(), ENGINE.tokenize("!is:nonsense"))


def test_is_comma_values_or():
    assert ENGINE._matches(_msg(flags=MessageFlags.DELETED),
                           ENGINE.tokenize("is:deleted,highlighted"))


# -- composition ----------------------------------------------------------------------------


def test_predicates_are_and():
    msg = _msg(flags=MessageFlags.HIGHLIGHTED)
    assert ENGINE._matches(msg, ENGINE.tokenize("from:alice is:highlighted"))
    assert not ENGINE._matches(msg, ENGINE.tokenize("from:alice is:deleted"))


def test_unknown_name_value_is_literal_substring():
    assert ENGINE._matches(_msg(text="see foo:bar here"), ENGINE.tokenize("foo:bar"))
    assert not ENGINE._matches(_msg(), ENGINE.tokenize("foo:bar"))


def test_search_returns_matching_in_order():
    messages = [
        _msg("one", msg_id="1"),
        _msg("two link https://x.com", msg_id="2"),
        _msg("three", msg_id="3"),
    ]
    assert [m.id for m in ENGINE.search(messages, "has:link")] == ["2"]
    assert [m.id for m in ENGINE.search(messages, "three")] == ["3"]
    assert ENGINE.search(messages, "") == []


def test_search_system_rows_excluded_from_from():
    sys_msg = _msg(name=None, author=None, flags=MessageFlags.SYSTEM, badges=[])
    assert ENGINE.search([sys_msg], "from:alice") == []
