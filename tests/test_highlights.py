"""Highlights/mentions: matcher semantics + ingress controller."""

import datetime as dt

import pytest

from chattersyn.config.settings import Settings
from chattersyn.core.bus import bridge
from chattersyn.models.message import Author, Message, MessageFlags, Platform
from chattersyn.services import highlights as highlights_mod
from chattersyn.services.highlights import (
    PING_PATH,
    HighlightController,
    HighlightMatcher,
)


def _msg(text, **kwargs):
    kw = dict(
        id="m1",
        platform=Platform.TWITCH,
        channel_id="forsen",
        ts=dt.datetime(2026, 10, 16, 9, 5, 7),
        author=Author(name="Alice", login="alice"),
        plain=text,
    )
    kw.update(kwargs)
    return Message(**kw)


def _entry(text, **overrides):
    entry = {"text": text, "regex": False, "case_sensitive": False,
             "color": "#ff0000", "alert": False, "sound": False}
    entry.update(overrides)
    return entry


def _settings(**overrides):
    return Settings({"highlights": [], "highlight_color": "#123456", **overrides})


# -- matcher: highlight entries -------------------------------------------------------


def test_plain_substring_case_insensitive():
    matcher = HighlightMatcher(_settings(highlights=[_entry("kappa")]))
    assert matcher.match(_msg("KAPPA 123")).kind == "highlight"


def test_case_sensitive_entry():
    matcher = HighlightMatcher(
        _settings(highlights=[_entry("Kappa", case_sensitive=True)]))
    assert matcher.match(_msg("kappa")) is None
    assert matcher.match(_msg("Kappa")).kind == "highlight"


def test_regex_entry_case_insensitive_by_default():
    matcher = HighlightMatcher(_settings(highlights=[_entry("^h[ae]y", regex=True)]))
    assert matcher.match(_msg("HEY!")).kind == "highlight"
    assert matcher.match(_msg("say hey")) is None  # ^ anchor


def test_regex_entry_honours_case_sensitive():
    matcher = HighlightMatcher(_settings(highlights=[_entry(
        "Kappa", regex=True, case_sensitive=True)]))
    assert matcher.match(_msg("kappa")) is None
    assert matcher.match(_msg("Kappa")).kind == "highlight"


def test_invalid_regex_entry_ignored():
    matcher = HighlightMatcher(_settings(highlights=[_entry("(", regex=True)]))
    assert matcher.match(_msg("(")) is None


def test_empty_text_entry_ignored():
    matcher = HighlightMatcher(_settings(highlights=[_entry("")]))
    assert matcher.match(_msg("hello")) is None


def test_non_dict_entries_ignored():
    matcher = HighlightMatcher(_settings(highlights=[{"text": "x"}, "junk", None]))
    assert matcher.match(_msg("x")).kind == "highlight"


def test_entry_hit_carries_color_alert_sound():
    matcher = HighlightMatcher(_settings(highlights=[_entry(
        "kappa", color="#00ff00", alert=True, sound=True)]))
    hit = matcher.match(_msg("kappa"))
    assert (hit.kind, hit.color, hit.alert, hit.sound) == ("highlight", "#00ff00", True, True)


def test_first_matching_entry_wins():
    matcher = HighlightMatcher(_settings(highlights=[
        _entry("hello", color="#ff0000"),
        _entry("hello", color="#00ff00"),
    ]))
    assert matcher.match(_msg("hello")).color == "#ff0000"


def test_empty_color_falls_back_to_global():
    matcher = HighlightMatcher(_settings(highlights=[_entry("kappa", color="")]))
    assert matcher.match(_msg("kappa")).color == "#123456"


def test_entries_match_system_rows():
    matcher = HighlightMatcher(_settings(highlights=[_entry("timed out")]))
    sys_msg = Message.system(Platform.TWITCH, "forsen", "bob was timed out")
    assert matcher.match(sys_msg).kind == "highlight"


def test_matcher_refreshes_on_settings_change():
    settings = _settings()
    matcher = HighlightMatcher(settings)
    assert matcher.match(_msg("kappa")) is None
    settings.set("highlights", [_entry("kappa")])
    assert matcher.match(_msg("kappa")).kind == "highlight"


# -- matcher: mentions ------------------------------------------------------------------


def _mention_settings(**notifications):
    return _settings(notifications={"mentions": True, "sound": True, "live": False,
                                    **notifications})


def test_mention_matches_login():
    matcher = HighlightMatcher(_mention_settings())
    hit = matcher.match(_msg("hey forsen what's up", author=Author(name="Bob", login="bob")),
                        self_login="forsen")
    assert hit is not None and hit.kind == "mention"
    assert hit.color == "#123456"       # global colour for mentions
    assert hit.sound is True            # notifications.sound drives the ping
    assert hit.alert is False


def test_mention_matches_display_name():
    matcher = HighlightMatcher(_mention_settings())
    hit = matcher.match(_msg("whats up TonyTwo"), self_login="tony2", self_name="TonyTwo")
    assert hit is not None and hit.kind == "mention"


def test_mention_disabled():
    matcher = HighlightMatcher(_mention_settings(mentions=False))
    assert matcher.match(_msg("hey forsen"), self_login="forsen") is None


def test_mention_without_identity():
    matcher = HighlightMatcher(_mention_settings())
    assert matcher.match(_msg("hey forsen"), self_login="") is None


def test_own_message_is_not_a_mention():
    matcher = HighlightMatcher(_mention_settings())
    own = _msg("kappa forsen", author=Author(name="Forsen", login="forsen"))
    assert matcher.match(own, self_login="forsen") is None


def test_mention_skips_system_rows():
    matcher = HighlightMatcher(_mention_settings())
    sys_msg = Message.system(Platform.TWITCH, "forsen", "forsen was timed out")
    assert matcher.match(sys_msg, self_login="forsen") is None


def test_mention_beats_highlight_entries():
    settings = Settings({
        "highlights": [_entry("kappa")],
        "highlight_color": "#123456",
        "notifications": {"mentions": True, "sound": False, "live": False},
    })
    matcher = HighlightMatcher(settings)
    hit = matcher.match(_msg("kappa forsen"), self_login="forsen")
    assert hit.kind == "mention"  # the entry matches too, but mention wins


# -- controller -------------------------------------------------------------------------


@pytest.fixture
def controller(qapp, monkeypatch):
    monkeypatch.setattr(highlights_mod.HighlightController, "_make_ping",
                        staticmethod(lambda: None))
    inst = HighlightController(
        _settings(highlights=[_entry("kappa", color="#ff0000", alert=True, sound=True)]))
    yield inst
    inst.stop()


class _SpyPing:
    def __init__(self):
        self.plays = 0

    def play(self):
        self.plays += 1


def test_flags_store_and_signal(controller):
    controller.start()
    got = []
    controller.highlighted.connect(lambda msg: got.append(msg.id))
    msg = _msg("kappa kappa")
    bridge.message.emit(msg)
    assert msg.flags & MessageFlags.HIGHLIGHTED
    assert msg.extra.get("highlight_color") == "#ff0000"
    assert controller.messages() == [msg]
    assert got == ["m1"]


def test_identity_provider_mention(controller):
    controller.set_identity_provider(lambda: Author(name="Tony", login="tony"))
    controller.start()
    msg = _msg("hello tony", id="m-mention", author=Author(name="Bob", login="bob"))
    bridge.message.emit(msg)
    assert msg.flags & MessageFlags.HIGHLIGHTED
    assert msg.extra.get("highlight_color") == "#123456"  # mention: global colour
    assert [m.id for m in controller.messages()] == ["m-mention"]


def test_sound_cooldown_swallows_burst(controller):
    controller.start()
    spy = _SpyPing()
    controller._ping = spy  # after start(): start() installs its own effect
    bridge.message.emit(_msg("kappa"))
    bridge.message.emit(_msg("kappa again"))
    assert spy.plays == 1  # second hit within the cooldown doesn't stack a ping


def test_sound_cooldown_zero_plays_every_hit(controller, monkeypatch):
    monkeypatch.setattr(highlights_mod, "_SOUND_COOLDOWN", 0.0)
    controller.start()
    spy = _SpyPing()
    controller._ping = spy
    bridge.message.emit(_msg("kappa"))
    bridge.message.emit(_msg("kappa 2"))
    assert spy.plays == 2


def test_alert_flashes_window(controller, monkeypatch):
    alerts = []
    monkeypatch.setattr(highlights_mod.QApplication, "alert",
                        lambda window, msec: alerts.append(window))
    window = object()
    controller.start(window=window)
    bridge.message.emit(_msg("kappa"))
    assert alerts == [window]


def test_store_bounded_to_scrollback(qapp, monkeypatch):
    monkeypatch.setattr(highlights_mod.HighlightController, "_make_ping",
                        staticmethod(lambda: None))
    inst = HighlightController(Settings({"scrollback": 2, "highlights": [_entry("x")]}))
    inst.start()
    for i in range(3):
        bridge.message.emit(_msg(f"x {i}", id=f"m{i}"))
    assert [m.id for m in inst.messages()] == ["m2", "m1"]
    inst.stop()


def test_clear_empties_store(controller):
    controller.start()
    bridge.message.emit(_msg("kappa"))
    controller.clear()
    assert controller.messages() == []


def test_stop_disconnects(controller):
    controller.start()
    controller.stop()
    msg = _msg("kappa")
    bridge.message.emit(msg)
    assert not msg.flags & MessageFlags.HIGHLIGHTED


def test_ping_asset_is_a_valid_wav():
    data = PING_PATH.read_bytes()
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
