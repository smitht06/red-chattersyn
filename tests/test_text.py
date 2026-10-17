"""Text utilities: UTF-16 index math, emotes tag parsing, URL detection."""

from chattersyn.core.text import (
    contains_url,
    emote_tokens,
    parse_emotes_tag,
    truncate_codepoints,
    utf16_len,
    utf16_range_to_py,
)


def test_utf16_len():
    assert utf16_len("abc") == 3
    assert utf16_len("a😀b") == 4  # astral char = surrogate pair


def test_utf16_range_bmp():
    assert utf16_range_to_py("hello world", 0, 5) == (0, 5)
    assert utf16_range_to_py("hello world", 6, 11) == (6, 11)


def test_utf16_range_astral():
    s = "a😀bc"
    # UTF-16 units: a=0, 😀=1..2 (surrogate pair), b=3, c=4
    assert utf16_range_to_py(s, 1, 3) == (1, 2)  # the emoji
    assert utf16_range_to_py(s, 3, 4) == (2, 3)  # 'b'


def test_parse_emotes_tag_ascii():
    text = "hi Kappa there"
    assert parse_emotes_tag(text, "25:3-7") == [("25", 3, 8)]


def test_parse_emotes_tag_action():
    text = "\x01ACTION waves Kappa\x01"
    # Tag units are relative to the FULL message (incl. the 8-unit ACTION
    # prefix): "waves " = units 8-13, so Kappa is at units 14-18 inclusive,
    # i.e. Python indices [14, 19) into the full text.
    assert parse_emotes_tag(text, "25:14-18") == [("25", 14, 19)]


def test_parse_emotes_tag_astral():
    text = "😀 Kappa"
    # units: 😀=0..1, space=2, Kappa=3..7 inclusive
    assert parse_emotes_tag(text, "25:3-7") == [("25", 2, 7)]


def test_parse_emotes_tag_multiple():
    text = "LUL and Kappa"
    result = parse_emotes_tag(text, "1:0-2/2:8-12")
    assert result == [("1", 0, 3), ("2", 8, 13)]


def test_truncate_codepoints():
    assert truncate_codepoints("12345", 3) == "123"
    assert truncate_codepoints("123", 5) == "123"


def test_contains_url():
    assert contains_url("check https://example.com/a")
    assert contains_url("visit www.google.com now")
    assert not contains_url("plain text")


def test_emote_tokens():
    assert emote_tokens("KEKW, LUL") == ["KEKW,", "KEKW", "LUL"]
    assert emote_tokens(":) hi") == [":)", "hi"]
