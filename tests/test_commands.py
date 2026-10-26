"""Command system: placeholders, user commands, builtins, pass-through, persistence."""

import datetime as dt
import json

import pytest

from chattersyn.config.settings import Settings
from chattersyn.models.message import Author, Message, Platform
from chattersyn.services.commands import (
    CommandContext,
    CommandController,
    ModAction,
    UserCommand,
    expand_placeholders,
    load_user_commands,
    save_user_commands,
)

WORDS = ["alpha", "beta"]
VARS = {"channel.name": "forsen", "my.name": "", "my.id": "me123"}


def _ctx(**kwargs):
    kw = dict(twitch_channel="forsen")
    kw.update(kwargs)
    return CommandContext(**kw)


def _controller(*commands):
    return CommandController(Settings({}), list(commands))


def _msg(id="m9", login="bob"):
    return Message(
        id=id,
        platform=Platform.TWITCH,
        channel_id="forsen",
        ts=dt.datetime(2026, 10, 16, 9, 5, 7),
        author=Author(name="Bob", login=login),
        plain="hi",
    )


# -- placeholders ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("hello {1}", "hello alpha"),
        ("{2+} end", "beta end"),
        ("{1} {2}", "alpha beta"),
        ("x{1}y", "xalphay"),
        ("{0} {5}", " "),                        # out-of-range -> empty string
        ("{1;x}", "{1;x}"),                      # digits take no fallback: literal
        ("{empty;fallback!} end", "{empty;fallback!} end"),  # unknown name: literal
        ("{my.name;you} {channel.name}", "you forsen"),      # empty var -> fallback
        ("{{1} escapes {2}", "{1} escapes beta"),
        ("{{{1}", "{{1}"),                      # one brace level stripped
        ("a{{1}b{2}c", "a{1}bbetac"),
    ],
)
def test_expand_placeholders(template, expected):
    assert expand_placeholders(template, WORDS, VARS) == expected


def test_expand_placeholders_empty_variable_no_fallback():
    assert expand_placeholders("{my.name}", WORDS, VARS) == ""


# -- user commands ------------------------------------------------------------------------


def test_user_command_shadows_builtin():
    ctrl = _controller(UserCommand("reply", "NO U {1+}"))
    outcome = ctrl.dispatch("/reply bob hi", _ctx())
    assert outcome.handled
    assert outcome.send_text == "NO U bob hi"
    assert outcome.reply_parent_id == ""  # the builtin /reply never ran


def test_user_command_typed_with_prefix():
    ctrl = _controller(UserCommand("hello", "HeyGuys {1+}"))
    assert ctrl.dispatch("/hello world", _ctx()).send_text == "HeyGuys world"


def test_user_command_bare_typing():
    ctrl = _controller(UserCommand("hello", "HeyGuys {1+}"))
    assert ctrl.dispatch("hello world", _ctx()).send_text == "HeyGuys world"


def test_user_command_own_prefix_in_name():
    ctrl = _controller(UserCommand("/slashy", "out"))
    assert ctrl.dispatch("/slashy", _ctx()).send_text == "out"


def test_user_command_multi_word_trigger():
    ctrl = _controller(UserCommand("say hello", "said: {1}"))
    outcome = ctrl.dispatch("/say hello there", _ctx())
    assert outcome.send_text == "said: there"


def test_user_command_case_sensitive_trigger():
    ctrl = _controller(UserCommand("HELLO", "upper"))
    outcome = ctrl.dispatch("/hello", _ctx())
    assert outcome.system == "Unknown command: /hello"


def test_user_func_emitting_builtin_reruns_pipeline():
    ctrl = _controller(UserCommand("a", "/b done"), UserCommand("b", "out {1+}"))
    assert ctrl.dispatch("/a", _ctx()).send_text == "out done"


def test_user_command_recursion_limit():
    ctrl = _controller(UserCommand("loop", "/loop x"))
    outcome = ctrl.dispatch("/loop", _ctx())
    assert outcome.handled
    assert outcome.system == "Command recursion limit reached."


def test_dry_run_previews_user_substitution():
    ctrl = _controller(UserCommand("hello", "HeyGuys {1+}"))
    outcome = ctrl.dispatch("/hello world", _ctx(), dry_run=True)
    assert outcome.handled
    assert outcome.send_text == "HeyGuys world"


def test_dry_run_recognizes_builtin_but_does_not_execute():
    cleared = []
    ctrl = _controller()
    outcome = ctrl.dispatch("/clearmessages", _ctx(clear_view=lambda: cleared.append(1)),
                            dry_run=True)
    assert outcome.handled
    assert cleared == []


# -- builtins ------------------------------------------------------------------------------


def test_reply_found():
    seen = []
    parent = _msg()
    ctrl = _controller()
    outcome = ctrl.dispatch(
        "/reply @BOB hello there", _ctx(find_last_from=lambda login: (seen.append(login), parent)[1]))
    assert seen == ["bob"]  # @ stripped, lowercased
    assert outcome.send_text == "hello there"
    assert outcome.reply_parent_id == "m9"
    assert outcome.reply_parent_login == "bob"


def test_reply_not_found():
    ctrl = _controller()
    outcome = ctrl.dispatch("/reply bob hi", _ctx(find_last_from=lambda login: None))
    assert outcome.system == "A message from that user wasn't found."


def test_reply_usage():
    ctrl = _controller()
    outcome = ctrl.dispatch("/reply bob", _ctx(find_last_from=lambda login: _msg()))
    assert outcome.system == "Usage: /reply <username> <message>"


def test_reply_twitch_only():
    ctrl = _controller()
    outcome = ctrl.dispatch("/reply bob hi", _ctx(twitch_channel=""))
    assert outcome.system == "The /reply command only works in Twitch channels."


def test_clear_sends_server_command_twitch_only():
    ctrl = _controller()
    outcome = ctrl.dispatch("/clear", _ctx())
    assert outcome.handled
    assert outcome.send_text == "/clear"
    assert outcome.twitch_only


def test_clear_twitch_only_guard():
    ctrl = _controller()
    outcome = ctrl.dispatch("/clear", _ctx(twitch_channel=""))
    assert outcome.system == "The /clear command only works in Twitch channels."


def test_clearmessages_clears_view():
    cleared = []
    ctrl = _controller()
    outcome = ctrl.dispatch("/clearmessages", _ctx(clear_view=lambda: cleared.append(1)))
    assert outcome.handled
    assert cleared == [1]
    assert outcome.send_text == ""


def test_copy():
    copied = []
    ctrl = _controller()
    outcome = ctrl.dispatch("/copy some text", _ctx(copy_text=copied.append))
    assert outcome.handled
    assert copied == ["some text"]


def test_openurl_with_flags():
    opened = []
    ctrl = _controller()
    outcome = ctrl.dispatch("/openurl --some-flag https://example.com",
                            _ctx(open_url=opened.append))
    assert outcome.handled
    assert opened == ["https://example.com"]


def test_openurl_rejects_non_http():
    opened = []
    ctrl = _controller()
    outcome = ctrl.dispatch("/openurl notaurl", _ctx(open_url=opened.append))
    assert opened == []
    assert outcome.system == "Usage: /openurl <http(s):// URL>"


def test_popout_default_channel():
    opened = []
    ctrl = _controller()
    ctrl.dispatch("/popout", _ctx(open_url=opened.append))
    assert opened == ["https://www.twitch.tv/popout/forsen/chat"]


def test_popout_named_channel():
    opened = []
    ctrl = _controller()
    ctrl.dispatch("/popout #foo", _ctx(open_url=opened.append))
    assert opened == ["https://www.twitch.tv/popout/foo/chat"]


def test_user_usage():
    ctrl = _controller()
    outcome = ctrl.dispatch("/user", _ctx(open_url=lambda u: None))
    assert outcome.system == "Usage: /user <username>"


def test_user_opens_profile():
    opened = []
    ctrl = _controller()
    ctrl.dispatch("/user @bob", _ctx(open_url=opened.append))
    assert opened == ["https://www.twitch.tv/bob"]


@pytest.mark.parametrize("name", ["poll", "prediction", "mods", "pin"])
def test_mod_stubs(name):
    ctrl = _controller()
    outcome = ctrl.dispatch(f"/{name} somebody", _ctx())
    assert outcome.handled
    assert outcome.system == f"The /{name} command is not implemented yet."


@pytest.mark.parametrize("name", ["announcepurple", "slow"])
def test_irc_mod_commands_pass_through(name):
    """Non-ban moderation builtins go out as IRC PRIVMSG (Twitch-only)."""
    ctrl = _controller()
    outcome = ctrl.dispatch(f"/{name} somebody", _ctx())
    assert outcome.handled
    assert outcome.send_text == f"/{name} somebody"
    assert outcome.twitch_only


def test_irc_mod_commands_accept_mixed_case():
    """'/Ban' must behave exactly like '/ban' — dispatch lowercases only the
    registry lookup, and the handler must not KeyError on the raw case."""
    ctrl = _controller()
    for text, expected in (("/Ban bob", ModAction("ban", "bob")),
                           ("/TimeOut bob 10m", ModAction("ban", "bob", duration=600)),
                           ("/UNBAN bob", ModAction("unban", "bob"))):
        outcome = ctrl.dispatch(text, _ctx())
        assert outcome.handled
        assert outcome.twitch_mod == expected
    assert ctrl.dispatch("/Slow 30", _ctx()).send_text == "/slow 30"


def test_ban_family_becomes_helix_actions():
    """ban/banid/unban/untimeout never go out as chat text — Twitch removed
    the IRC forms, so they'd be posted as plain messages or rejected."""
    ctrl = _controller()
    cases = {
        "/ban bob": ModAction("ban", "bob"),
        "/banid 12345": ModAction("ban", "12345", by_id=True),
        "/unban @bob": ModAction("unban", "bob"),
        "/untimeout bob": ModAction("unban", "bob"),
    }
    for text, expected in cases.items():
        outcome = ctrl.dispatch(text, _ctx())
        assert outcome.twitch_mod == expected
        assert outcome.send_text == ""
    assert ctrl.dispatch("/ban bob", _ctx(twitch_channel="")).twitch_mod is None


def test_timeout_validates_duration_and_strips_at():
    ctrl = _controller()
    outcome = ctrl.dispatch("/timeout @bob 5m", _ctx())
    assert outcome.handled
    assert outcome.twitch_mod == ModAction("ban", "bob", duration=300)
    bad = ctrl.dispatch("/timeout bob forever", _ctx())
    assert bad.system == "Invalid timeout duration: forever"


def test_timeout_defaults_to_10_minutes():
    """No duration -> Chatterino's default 600s (docs/m4.md); Twitch rejects
    a duration-less /timeout with a usage NOTICE."""
    ctrl = _controller()
    outcome = ctrl.dispatch("/timeout bob", _ctx())
    assert outcome.twitch_mod == ModAction("ban", "bob", duration=600)
    with_reason = ctrl.dispatch("/timeout bob 10m spam", _ctx())
    assert with_reason.twitch_mod == ModAction("ban", "bob", duration=600, reason="spam")


def test_timeout_accepts_combined_durations():
    """Combos like 1d2h are documented (docs/m4.md) and accepted by Twitch."""
    ctrl = _controller()
    outcome = ctrl.dispatch("/timeout bob 1d2h", _ctx())
    assert outcome.twitch_mod.duration == 93600
    more = ctrl.dispatch("/timeout bob 1w2d3h4m5s", _ctx())
    assert more.twitch_mod.duration == 604800 + 2 * 86400 + 3 * 3600 + 4 * 60 + 5


def test_warn_and_commercial_accept_documented_args():
    ctrl = _controller()
    assert ctrl.dispatch("/warn bob spam", _ctx()).send_text == "/warn bob spam"
    assert ctrl.dispatch("/commercial 60", _ctx()).send_text == "/commercial 60"
    assert ctrl.dispatch("/ban bob annoying", _ctx()).twitch_mod == ModAction(
        "ban", "bob", reason="annoying")


def test_mod_commands_accept_multi_word_reasons():
    """The reason tail is free text (Twitch joins the remaining words) —
    the arity cap must not reject it."""
    ctrl = _controller()
    assert (ctrl.dispatch("/timeout bob 10m Bad word detected", _ctx()).twitch_mod
            == ModAction("ban", "bob", duration=600, reason="Bad word detected"))
    assert (ctrl.dispatch("/ban bob annoying more words", _ctx()).twitch_mod
            == ModAction("ban", "bob", reason="annoying more words"))
    assert (ctrl.dispatch("/warn bob spam spam spam", _ctx()).send_text
            == "/warn bob spam spam spam")


def test_timeout_duration_range_validated():
    """Twitch accepts 1..1209600 s (2 weeks, docs/m4.md): shape-valid but
    out-of-range durations get a client-side error instead of a NOTICE."""
    ctrl = _controller()
    assert ctrl.dispatch("/timeout bob 0", _ctx()).system == \
        "Invalid timeout duration: 0 (1s – 2w)"
    assert ctrl.dispatch("/timeout bob 999w", _ctx()).system == \
        "Invalid timeout duration: 999w (1s – 2w)"
    assert ctrl.dispatch("/timeout bob 2w", _ctx()).twitch_mod.duration == 1209600
    assert ctrl.dispatch("/timeout bob 1", _ctx()).twitch_mod.duration == 1
    assert ctrl.dispatch("/timeout bob 30s", _ctx()).twitch_mod.duration == 30


def test_timeout_zero_unit_duration_rejected_not_crash():
    """A unit-bearing duration summing to zero ('0s') gets the range error,
    not a ValueError from int('0s') (regression: the bare-digit fallback
    ran on unit strings whenever the sum was 0)."""
    ctrl = _controller()
    assert ctrl.dispatch("/timeout bob 0s", _ctx()).system == \
        "Invalid timeout duration: 0s (1s – 2w)"
    assert ctrl.dispatch("/timeout bob 0m0s", _ctx()).system == \
        "Invalid timeout duration: 0m0s (1s – 2w)"


def test_timeout_rejects_trailing_digit_without_unit():
    """'2w3' is malformed (every number in a combo carries a unit) — reject
    it instead of silently dropping the '3' from the range check and
    forwarding the raw text to Twitch."""
    ctrl = _controller()
    assert ctrl.dispatch("/timeout bob 2w3", _ctx()).system == \
        "Invalid timeout duration: 2w3"
    assert ctrl.dispatch("/timeout bob 1s2", _ctx()).system == \
        "Invalid timeout duration: 1s2"


def test_timeout_rejects_unicode_digits_and_huge_values():
    """ASCII digits only (Twitch rejects Unicode ones), and pathological
    digit counts must not crash dispatch on Python's int() cap."""
    ctrl = _controller()
    assert ctrl.dispatch("/timeout bob ١m", _ctx()).system == \
        "Invalid timeout duration: ١m"
    huge = "9" * 4301  # exceeds int()'s 4300-digit conversion cap
    assert ctrl.dispatch(f"/timeout bob {huge}", _ctx()).system == \
        f"Invalid timeout duration: {huge} (1s – 2w)"


def test_warn_requires_reason():
    """docs/m4.md: /warn <username> <reason> — Twitch rejects a reason-less
    /warn with a usage NOTICE, so the client must require it up front."""
    ctrl = _controller()
    assert ctrl.dispatch("/warn bob", _ctx()).system == \
        "Usage: /warn <username> <reason>"
    assert ctrl.dispatch("/warn bob stop it", _ctx()).send_text == "/warn bob stop it"


def test_mod_stubs_registered_for_autocomplete():
    names = _controller().builtin_names()
    for name in ("/ban", "/timeout", "/vip", "/shoutout", "/poll", "/prediction"):
        assert name in names
    assert "/reply" in names


def test_unknown_command_blocked():
    ctrl = _controller()
    assert ctrl.dispatch("/doesnotexist", _ctx()).system == "Unknown command: /doesnotexist"
    assert ctrl.dispatch(".wat", _ctx()).system == "Unknown command: .wat"


@pytest.mark.parametrize("text", [
    "/me dances", ".me dances", "/help", "/disconnect", "/host foo", "/unhost foo",
])
def test_pass_through_to_twitch(text):
    outcome = _controller().dispatch(text, _ctx())
    assert not outcome.handled
    assert outcome.send_text == ""


def test_plain_text_not_a_command():
    outcome = _controller().dispatch("just chatting", _ctx())
    assert not outcome.handled


def test_empty_input_not_a_command():
    assert not _controller().dispatch("", _ctx()).handled


def test_mentions_builtin_invokes_callback():
    opened = []
    outcome = _controller().dispatch("/mentions", _ctx(open_mentions=lambda: opened.append(1)))
    assert opened == [1]
    assert outcome.handled
    assert not outcome.send_text and not outcome.system


def test_mentions_builtin_without_callback_is_handled():
    outcome = _controller().dispatch("/mentions", _ctx())
    assert outcome.handled


# -- commands.json persistence -------------------------------------------------------------


def test_save_load_round_trip(tmp_path):
    commands = [
        UserCommand("hello", "HeyGuys {1+}", show_in_context_menu=True),
        UserCommand("say hello", "said: {1}"),
    ]
    save_user_commands(tmp_path, commands)
    assert load_user_commands(tmp_path) == commands
    data = json.loads((tmp_path / "commands.json").read_text(encoding="utf-8"))
    assert data["commands"][0] == {
        "name": "hello", "func": "HeyGuys {1+}", "showInMsgContextMenu": True}


def test_load_missing_file_is_empty(tmp_path):
    assert load_user_commands(tmp_path) == []


def test_load_invalid_json_is_empty(tmp_path):
    (tmp_path / "commands.json").write_text("{not json", encoding="utf-8")
    assert load_user_commands(tmp_path) == []


def test_load_filters_invalid_entries(tmp_path):
    (tmp_path / "commands.json").write_text(json.dumps({
        "commands": [
            {"name": "ok", "func": "fine"},
            {"name": "no-func"},       # missing func
            {"func": "no-name"},       # missing name
            "not-a-dict",
        ],
    }), encoding="utf-8")
    assert load_user_commands(tmp_path) == [UserCommand("ok", "fine")]


def test_from_json_requires_name_and_func():
    assert UserCommand.from_json({}) is None
    assert UserCommand.from_json({"name": "", "func": "x"}) is None
    assert UserCommand.from_json({"name": "a", "func": "x"}) == UserCommand("a", "x")
