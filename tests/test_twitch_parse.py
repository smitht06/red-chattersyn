"""IRCv3 line/tag parsing + event mapping against the fixture corpus."""

from pathlib import Path

from chattersyn.services.twitch import parse
from chattersyn.services.twitch.mapper import TwitchMapper

FIXTURES = Path(__file__).parent / "fixtures" / "twitch_irc_lines.txt"


def _corpus_lines():
    lines = []
    for raw in FIXTURES.read_text(encoding="utf-8").splitlines():
        if not raw or raw.startswith("#"):
            continue
        # The corpus stores CTCP control chars as \x01 escapes so it stays
        # readable in git; unescape before parsing.
        lines.append(raw.replace("\\x01", "\x01"))
    return lines


# -- tag unescaping -------------------------------------------------------------


def test_unescape_tag():
    assert parse.unescape_tag(r"Hello\sworld") == "Hello world"
    assert parse.unescape_tag(r"a\:b\sc\\d") == "a;b c\\d"


def test_parse_tags_escaped_spaces():
    tags = parse.parse_tags(r"display-name=Forsen\s;color=#FF0000")
    # Trailing escaped space is kept (only stripped for the LAST tag).
    assert tags["display-name"] == "Forsen "
    tags = parse.parse_tags(r"color=#FF0000;display-name=Forsen\s")
    assert tags["display-name"] == "Forsen"


def test_parse_tags_empty_and_valueless():
    tags = parse.parse_tags("badge-info=;badges=;flags=")
    assert tags == {"badge-info": "", "badges": "", "flags": ""}


# -- line splitting ---------------------------------------------------------------


def test_parse_privmsg():
    msg = parse.parse_irc_line(
        "@badges=moderator/1;color=#00FF7F :moduser!moduser@moduser.tmi.twitch.tv "
        "PRIVMSG #forsen :hello :world"
    )
    assert msg.command == "PRIVMSG"
    assert msg.params == ["#forsen", "hello :world"]  # internal ':' kept
    assert msg.prefix == "moduser!moduser@moduser.tmi.twitch.tv"
    assert msg.tags["badges"] == "moderator/1"


def test_parse_ping_pong():
    assert parse.parse_irc_line("PING :tmi.twitch.tv").params == ["tmi.twitch.tv"]
    assert parse.parse_irc_line("PONG :tmi.twitch.tv").params == ["tmi.twitch.tv"]


def test_parse_no_params_command():
    msg = parse.parse_irc_line("CAP LS 302")
    assert msg.command == "CAP"
    assert msg.params == ["LS", "302"]


def test_parse_353():
    msg = parse.parse_irc_line(":tmi.twitch.tv 353 justinfan12345 = #forsen :a b c")
    assert msg.command == "353"
    assert msg.params == ["justinfan12345", "=", "#forsen", "a b c"]


def test_parse_empty_line():
    assert parse.parse_irc_line("") is None


# -- corpus smoke: every line parses ----------------------------------------------


def test_corpus_all_lines_parse():
    for line in _corpus_lines():
        parsed = parse.parse_irc_line(line)
        assert parsed is not None, line
        assert parsed.command, line


def test_corpus_all_lines_map_without_exceptions():
    mapper = TwitchMapper()
    for line in _corpus_lines():
        parsed = parse.parse_irc_line(line)
        assert parsed is not None
        # Mapping must never raise, regardless of lookups being absent.
        mapper.map(parsed, "#forsen")
