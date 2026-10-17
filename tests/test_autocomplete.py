"""Pure autocomplete logic: word bounds, per-context matching, reply-prefix
handling, the 200 cap and the insert texts (docs/m4.md "Autocomplete")."""

from chattersyn.models.emote import Emote
from chattersyn.services.autocomplete import (
    MAX_CANDIDATES,
    build_candidates,
    match_commands,
    match_emojis,
    match_emotes,
    match_users,
    word_bounds,
)
from chattersyn.services.emojis import EmojiEntry


def _emote(name, **kwargs):
    kwargs.setdefault("provider", "7tv")
    return Emote(id=name, name=name, **kwargs)


def _entry(shortcode, char="x", category="Symbols"):
    return EmojiEntry(shortcode=shortcode, char=char, category=category)


# -- word bounds ------------------------------------------------------------------------


def test_word_bounds_middle_of_word():
    assert word_bounds("hello world", 7) == (6, 11)


def test_word_bounds_start_and_end():
    assert word_bounds("hello", 0) == (0, 0)  # nothing typed yet
    assert word_bounds("hello", 5) == (0, 5)  # cursor at the word's end is inside it


def test_word_bounds_cursor_on_space_or_outside():
    assert word_bounds("hi there", 2) == (0, 2)  # end boundary of "hi"
    assert word_bounds("hi there", 3) == (3, 3)  # on the space
    assert word_bounds("hi there", 99) == (99, 99)
    assert word_bounds("hi ", 3) == (3, 3)


def test_word_bounds_empty_text():
    assert word_bounds("", 0) == (0, 0)


# -- matchers ---------------------------------------------------------------------------


def test_match_users_prefix_only_case_insensitive_dedup():
    assert match_users("for", ["Forsen", "fors", "NymN", "Forsen"]) == ["Forsen", "fors"]
    assert match_users("nym", ["Forsen", "NymN"]) == ["NymN"]


def test_match_emotes_contains_with_exact_hoisted():
    emotes = [_emote("KEKW"), _emote("KEKWait"), _emote("LULW")]
    assert [e.name for e in match_emotes("kek", emotes)] == ["KEKW", "KEKWait"]
    # exact match sorts first even though it is alphabetically later
    emotes = [_emote("OmegaKEKW"), _emote("KEKW")]
    assert [e.name for e in match_emotes("kekw", emotes)] == ["KEKW", "OmegaKEKW"]


def test_match_emotes_tilde_restricts_to_zero_width():
    emotes = [_emote("OOO", zero_width=True), _emote("OO")]
    assert [e.name for e in match_emotes("~o", emotes)] == ["OOO"]
    assert {e.name for e in match_emotes("o", emotes)} == {"OOO", "OO"}


def test_match_emotes_dedup_by_lower_name():
    emotes = [_emote("Kappa", provider="bttv"), _emote("kappa", provider="ffz")]
    assert len(match_emotes("kappa", emotes)) == 1


def test_match_emojis_contains_with_exact_hoisted():
    entries = [_entry("joy"), _entry("joy_cat"), _entry("thumbs_up")]
    assert [e.shortcode for e in match_emojis("joy", entries)] == ["joy", "joy_cat"]


def test_match_commands_prefix_only_strips_sigils():
    commands = ["/ban", "/timeout", "/unban", ".clear"]
    assert match_commands("b", commands) == ["ban"]
    assert match_commands("", commands) == ["ban", "clear", "timeout", "unban"]


# -- build_candidates: contexts ----------------------------------------------------------


def test_at_users_popup():
    result = build_candidates(
        "@for", 4, users=["Forsen", "fors"], emotes=[], commands=[], emojis=())
    assert result is not None and result.popup
    assert (result.start, result.end) == (0, 4)
    assert [(c.name, c.insert, c.sub) for c in result.candidates] == [
        ("Forsen", "@Forsen ", "user"), ("fors", "@fors ", "user"),
    ]


def test_at_no_matches_returns_none():
    assert build_candidates("@z", 2, users=["Forsen"], emotes=[], commands=[], emojis=()) is None


def test_colon_serves_emotes_then_emojis():
    result = build_candidates(
        ":kek", 4, users=[], emotes=[_emote("KEKW"), _emote("KEKWait")],
        commands=[], emojis=(_entry("kekw_emoji"), _entry("joy")))
    assert result is not None and result.popup
    assert [c.name for c in result.candidates] == ["KEKW", "KEKWait", "kekw_emoji"]
    assert result.candidates[0].emote is not None
    assert result.candidates[0].sub == "7tv"
    assert result.candidates[2].glyph == "x"
    assert result.candidates[2].insert == ":kekw_emoji: "


def test_colon_emoji_only_when_no_emotes():
    result = build_candidates(
        ":jo", 3, users=[], emotes=[], commands=[], emojis=(_entry("joy", "😂"),))
    assert result is not None and result.popup
    assert [(c.name, c.insert) for c in result.candidates] == [("joy", ":joy: ")]


def test_colon_tilde_zero_width_in_popup():
    result = build_candidates(
        ":~oo", 4, users=[], emotes=[_emote("OOO", zero_width=True), _emote("OO")],
        commands=[], emojis=())
    assert result is not None
    assert [c.name for c in result.candidates] == ["OOO"]


def test_slash_command_first_word_no_popup():
    result = build_candidates(
        "/b", 2, users=[], emotes=[], commands=["/ban", "/timeout"], emojis=())
    assert result is not None and not result.popup
    assert [(c.name, c.insert, c.sub) for c in result.candidates] == [
        ("/ban", "/ban ", "command")]


def test_dot_command_first_word():
    result = build_candidates(
        ".b", 2, users=[], emotes=[], commands=[".ban"], emojis=())
    assert result is not None
    assert [c.name for c in result.candidates] == [".ban"]


def test_plain_word_unified_first_word():
    result = build_candidates(
        "kek", 3, users=["forsen"], emotes=[_emote("KEKW")],
        commands=["/kekw"], emojis=())
    assert result is not None and not result.popup
    assert [(c.name, c.insert) for c in result.candidates] == [
        ("KEKW", "KEKW "), ("kekw", "/kekw "),
    ]


def test_plain_word_mid_message_excludes_commands():
    result = build_candidates(
        "hi kek", 6, users=["keklord"], emotes=[_emote("KEKW")],
        commands=["/kekw"], emojis=())
    assert result is not None
    assert (result.start, result.end) == (3, 6)
    assert [c.name for c in result.candidates] == ["KEKW", "keklord"]


def test_plain_word_emotes_match_word_start_only():
    assert build_candidates(
        "hi wait", 7, users=[], emotes=[_emote("KEKWait")], commands=[], emojis=()) is None


def test_prefix_is_from_word_start_not_cursor():
    result = build_candidates(
        "kekw", 2, users=[], emotes=[_emote("KEKW")], commands=[], emojis=())
    # cursor sits after "ke" inside the word -> prefix "ke", bounds span it all
    assert result is not None and (result.start, result.end) == (0, 4)
    assert result.candidates[0].name == "KEKW"


def test_cursor_on_whitespace_returns_none():
    assert build_candidates(
        "hi ", 3, users=["hi"], emotes=[], commands=[], emojis=()) is None
    assert build_candidates("", 0, users=["hi"], emotes=[], commands=[], emojis=()) is None


def test_cap_at_200_candidates():
    users = [f"user{i:03d}" for i in range(300)]
    result = build_candidates("@u", 2, users=users, emotes=[], commands=[], emojis=())
    assert result is not None and len(result.candidates) == MAX_CANDIDATES == 200


# -- reply prefix ------------------------------------------------------------------------


def test_reply_prefix_ignored_for_first_word_and_offsets():
    result = build_candidates(
        "@Forsen /b", 10, users=[], emotes=[],
        commands=["/ban"], emojis=(), reply_prefix="@Forsen ")
    # the "/b" is still treated as first word, indices refer to the original text
    assert result is not None and not result.popup
    assert (result.start, result.end) == (8, 10)
    assert [c.name for c in result.candidates] == ["/ban"]


def test_cursor_inside_reply_prefix_returns_none():
    assert build_candidates(
        "@Forsen ", 3, users=["Forsen"], emotes=[], commands=[], emojis=(),
        reply_prefix="@Forsen ") is None


def test_reply_prefix_must_match_to_apply():
    result = build_candidates(
        "@Forsen", 7, users=["Forsen"], emotes=[], commands=[], emojis=(),
        reply_prefix="@Forsen ")
    assert result is not None and result.popup  # normal @-user context
