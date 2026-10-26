"""YouTube-side emotes: emotes are removed from the YouTube copy of a send
(or swapped for emoji when opted in), and emote runs render YouTube rows."""

from chattersyn.config.settings import Settings
from chattersyn.models.message import ImageRun, TextRun
from chattersyn.services.emotes.emote_registry import EmoteRegistry
from chattersyn.services.emotes.youtube_emoji import (
    DEFAULT_EMOJI,
    emoji_map,
    emote_runs,
    has_youtube_text,
    youtube_emote_text,
)

EMOJI_ON = Settings({"youtube_emotes": {"emoji": True}})


def _registry():
    registry = EmoteRegistry(Settings())
    registry.set_twitch_global_emotes({"1": "Kappa", "2": "KEKW"})
    registry.set_twitch_emotes("forsen", {"9": "forsenE"})
    return registry


def _yt(text, settings=None, registry="default", channel="forsen"):
    if registry == "default":
        registry = _registry()
    return youtube_emote_text(text, registry, channel, settings or Settings())


def test_emotes_are_removed_by_default():
    assert _yt("gg forsenE wp") == "gg wp"
    assert _yt("hi Kappa, nice KEKW!") == "hi , nice !"  # punctuation stays
    assert _yt("forsenE forsenE") == ""
    assert _yt("  spaced   text ") == "  spaced   text "  # untouched without emotes
    assert _yt("kappa stays") == "kappa stays"  # not an emote name (case-sensitive)


def test_channel_scoped_emotes_only_removed_in_their_channel():
    assert _yt("gg forsenE", channel="other") == "gg forsenE"


def test_without_registry_nothing_is_known_to_be_an_emote():
    assert _yt("gg forsenE KEKW", registry=None) == "gg forsenE KEKW"
    # ... but the opt-in emoji table still applies by name.
    assert _yt("gg KEKW", EMOJI_ON, registry=None) == "gg 🤣"


def test_emoji_option_maps_table_names_and_removes_the_rest():
    assert _yt("KEKW forsenE", EMOJI_ON) == "🤣"
    assert _yt("hi KEKW, love it <3", EMOJI_ON) == "hi 🤣, love it ❤️"
    assert _yt("(Kappa)", EMOJI_ON) == "(😏)"  # registry piece inside a word
    custom = Settings({"youtube_emotes": {"emoji": True, "map": {"forsenE": "🙃"}}})
    assert emoji_map(custom) == {"forsenE": "🙃"}  # replaces the defaults wholesale
    assert _yt("KEKW forsenE", custom) == "🙃"
    assert emoji_map(Settings()) == DEFAULT_EMOJI


def test_has_youtube_text():
    registry = _registry()
    assert has_youtube_text("gg Kappa", registry, "forsen", Settings())
    assert not has_youtube_text("Kappa KEKW", registry, "forsen", Settings())
    assert has_youtube_text("Kappa KEKW", registry, "forsen", EMOJI_ON)  # emoji survive


def test_emote_runs_renders_known_names_only():
    runs = emote_runs("gg Kappa forsenE lol", _registry(), "forsen")
    images = [run.emote.name for run in runs if isinstance(run, ImageRun)]
    assert images == ["Kappa", "forsenE"]
    assert "".join(run.text if isinstance(run, TextRun) else run.alt for run in runs) == \
        "gg Kappa forsenE lol"
    assert not any(isinstance(r, ImageRun) and r.emote.name == "forsenE"
                   for r in emote_runs("forsenE", _registry(), "other"))
    assert emote_runs("Kappa", None, "forsen") == [TextRun("Kappa")]


def test_glued_twitch_emotes_are_removed_like_twitch_renders_them():
    """Regression: three picker clicks insert `redcyb1Hiredcyb1Hiredcyb1Hi`.
    Twitch's copy is respaced into three emotes, so YouTube must get nothing
    (it used to get the whole glued word)."""
    registry = EmoteRegistry(Settings())
    registry.set_twitch_emotes("redcyberdragon29", {"1": "redcyb1Hi", "2": "redcyb1L"})
    ch = "redcyberdragon29"
    assert youtube_emote_text("redcyb1Hiredcyb1Hiredcyb1Hi", registry, ch, Settings()) == ""
    assert youtube_emote_text("hi redcyb1Lredcyb1L there", registry, ch, Settings()) == "hi there"
    assert not has_youtube_text("redcyb1Lredcyb1L", registry, ch, Settings())
    # A word that only partly matches stays text on Twitch, so it stays on YouTube.
    assert youtube_emote_text("redcyb1Hix", registry, ch, Settings()) == "redcyb1Hix"
