"""7TV / BTTV / FFZ providers and EmoteRegistry priority (respx-mocked)."""

import httpx
import respx

from chattersyn.config.settings import Settings
from chattersyn.models.emote import Emote
from chattersyn.services.emotes.bttv import BttvProvider
from chattersyn.services.emotes.emote_registry import EmoteRegistry
from chattersyn.services.emotes.ffz import FfzProvider
from chattersyn.services.emotes.seventv import SevenTvProvider

# -- 7TV ------------------------------------------------------------------------------


@respx.mock
async def test_seventv_channel_set():
    respx.get("https://7tv.io/v3/users/twitch/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "u1",
                "platform": "TWITCH",
                "username": "forsen",
                "emote_set": {"id": "set-9"},
            },
        )
    )
    respx.get("https://7tv.io/v3/emote-sets/set-9").mock(
        return_value=httpx.Response(
            200,
            json={
                "emotes": [
                    {
                        "id": "abc",
                        "name": "KEKW",
                        "flags": 0,
                        "data": {
                            "animated": True,
                            "host": {
                                "url": "//cdn.7tv.app",
                                "files": [
                                    {"name": "1x.webp", "width": 28, "height": 28},
                                    {"name": "2x.webp", "width": 56, "height": 56},
                                ],
                            },
                        },
                    },
                    {
                        "id": "zq",
                        "name": "hmm",
                        "flags": 1,  # zero-width
                        "data": {
                            "animated": False,
                            "host": {"url": "//cdn.7tv.app", "files": [{"name": "1x.webp"}]},
                        },
                    },
                ]
            },
        )
    )
    provider = SevenTvProvider()
    try:
        emotes = await provider.fetch_for_channel("forsen", "123")
    finally:
        await provider.close()
    kekw = next(e for e in emotes if e.name == "KEKW")
    assert kekw.animated and kekw.urls["2x"] == "https://cdn.7tv.app/2x.webp"
    hmm = next(e for e in emotes if e.name == "hmm")
    assert hmm.zero_width and not hmm.animated


@respx.mock
async def test_seventv_emote_sets_list_fallback():
    respx.get("https://7tv.io/v3/users/twitch/123").mock(
        return_value=httpx.Response(
            200, json={"emote_sets": [{"id": "set-1", "name": "forsen"}]}
        )
    )
    respx.get("https://7tv.io/v3/emote-sets/set-1").mock(
        return_value=httpx.Response(200, json={"emotes": []})
    )
    provider = SevenTvProvider()
    try:
        assert await provider.fetch_for_channel("forsen", "123") == []
    finally:
        await provider.close()


@respx.mock
async def test_seventv_error_is_empty_not_fatal():
    respx.get("https://7tv.io/v3/users/twitch/123").mock(
        return_value=httpx.Response(500)
    )
    provider = SevenTvProvider()
    try:
        assert await provider.fetch_for_channel("forsen", "123") == []
    finally:
        await provider.close()


# -- BTTV -----------------------------------------------------------------------------


@respx.mock
async def test_bttv_404_user_means_no_channel_emotes():
    respx.get("https://api.betterttv.net/3/cached/users/twitch/123").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.betterttv.net/3/cached/emotes/global").mock(
        return_value=httpx.Response(
            200, json=[{"id": "g1", "code": "LULW", "imageType": "png", "animated": False}]
        )
    )
    provider = BttvProvider()
    try:
        assert await provider.fetch_for_channel("forsen", "123") == []
        globals_ = await provider.fetch_global()
    finally:
        await provider.close()
    assert len(globals_) == 1
    assert globals_[0].urls["3x"] == "https://cdn.betterttv.net/emote/g1/3x"
    assert globals_[0].scope is None


@respx.mock
async def test_bttv_channel_and_global_kept_separate():
    respx.get("https://api.betterttv.net/3/cached/users/twitch/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "channelEmotes": [{"id": "c1", "code": "Pog", "imageType": "gif", "animated": True}],
                "sharedEmotes": [{"id": "c2", "code": "Nice", "imageType": "png", "animated": False}],
            },
        )
    )
    respx.get("https://api.betterttv.net/3/cached/emotes/global").mock(
        return_value=httpx.Response(
            200, json=[{"id": "g1", "code": "Pog", "imageType": "png", "animated": False}]
        )
    )
    provider = BttvProvider()
    try:
        channel = await provider.fetch_for_channel("forsen", "123")
        globals_ = await provider.fetch_global()
    finally:
        await provider.close()
    by_code = {e.name: e for e in channel}
    assert by_code["Pog"].id == "c1" and by_code["Pog"].animated
    assert set(by_code) == {"Pog", "Nice"}
    assert all(e.scope == "forsen" for e in channel)
    assert globals_[0].id == "g1" and globals_[0].scope is None


# -- FFZ ------------------------------------------------------------------------------


@respx.mock
async def test_ffz_room_set_filters_modifiers():
    respx.get("https://api.frankerfacez.com/v1/room/forsen").mock(
        return_value=httpx.Response(
            200,
            json={
                "room": {"_id": 1, "twitch_id": 123, "set": 4330},
                "sets": {
                    "4330": {
                        "_type": 1,
                        "emoticons": [
                            {
                                "id": 1,
                                "name": "ForsenE",
                                "height": 32,
                                "width": 36,
                                "modifier_flags": 0,
                                "urls": {"1": "//cdn.frankerfacez.com/emoticon/1/1", "2": "//cdn.frankerfacez.com/emoticon/1/2"},
                            },
                            {
                                "id": 2,
                                "name": "ZWF",
                                "height": 0,
                                "width": 0,
                                "modifier_flags": 1,  # zero-width: kept
                                "urls": {"1": "//cdn.frankerfacez.com/emoticon/2/1"},
                            },
                            {
                                "id": 3,
                                "name": "FlipE",
                                "height": 32,
                                "width": 36,
                                "modifier_flags": 2,  # flip: excluded from matching
                                "urls": {"1": "//cdn.frankerfacez.com/emoticon/3/1"},
                            },
                        ],
                    }
                },
            },
        )
    )
    provider = FfzProvider()
    try:
        emotes = await provider.fetch_for_channel("forsen", "123")
    finally:
        await provider.close()
    names = {e.name for e in emotes}
    assert names == {"ForsenE", "ZWF"}
    forsene = next(e for e in emotes if e.name == "ForsenE")
    assert forsene.urls["1x"] == "https://cdn.frankerfacez.com/emoticon/1/1"
    assert forsene.height_px == 32
    assert next(e for e in emotes if e.name == "ZWF").zero_width


@respx.mock
async def test_ffz_404_is_empty():
    respx.get("https://api.frankerfacez.com/v1/room/forsen").mock(
        return_value=httpx.Response(404)
    )
    provider = FfzProvider()
    try:
        assert await provider.fetch_for_channel("forsen", "123") == []
    finally:
        await provider.close()


# -- 7TV/FFZ global sets ---------------------------------------------------------------


@respx.mock
async def test_seventv_global_set():
    respx.get("https://7tv.io/v3/emote-sets/global").mock(
        return_value=httpx.Response(
            200,
            json={"emotes": [{"id": "g7", "name": "GIGA", "flags": 0,
                              "data": {"animated": False, "host": {
                                  "url": "//cdn.7tv.app",
                                  "files": [{"name": "1x.webp"}]}}}]},
        )
    )
    provider = SevenTvProvider()
    try:
        emotes = await provider.fetch_global()
    finally:
        await provider.close()
    assert emotes[0].name == "GIGA"
    assert emotes[0].scope is None


@respx.mock
async def test_ffz_global_set():
    respx.get("https://api.frankerfacez.com/v1/set/global").mock(
        return_value=httpx.Response(
            200,
            json={"set": {"id": 3, "title": "Global Emoticons", "emoticons": [
                {"id": 5, "name": "FeelsOkayMan", "height": 30, "modifier_flags": 0,
                 "urls": {"1": "//cdn.frankerfacez.com/emoticon/5/1"}}]}},
        )
    )
    provider = FfzProvider()
    try:
        emotes = await provider.fetch_global()
    finally:
        await provider.close()
    assert emotes[0].name == "FeelsOkayMan"
    assert emotes[0].scope is None


# -- registry -------------------------------------------------------------------------


def test_registry_priority_and_toggle():
    settings = Settings()
    registry = EmoteRegistry(settings)
    # 7TV and BTTV both know "KEKW" — 7TV comes first in the default priority.
    registry._channel = {
        "7tv": {"KEKW": Emote("a", "KEKW", "7tv", {"1x": "https://x/a.png"})},
        "bttv": {"KEKW": Emote("b", "KEKW", "bttv", {"1x": "https://x/b.png"})},
        "ffz": {"onlyffz": Emote("c", "onlyffz", "ffz", {"1x": "https://x/c.png"})},
    }
    assert registry.lookup("KEKW").provider == "7tv"
    assert registry.lookup("onlyffz").provider == "ffz"
    assert registry.lookup("nope") is None
    # Disabling BTTV must not reorder anything; disabling 7TV lets BTTV win.
    settings.set("emote_providers.7tv", False)
    assert registry.lookup("KEKW").provider == "bttv"


def test_registry_channel_shadows_global_within_provider():
    registry = EmoteRegistry(Settings())
    registry._channel = {
        "bttv": {"Pog": Emote("c1", "Pog", "bttv", {"1x": "https://x/c.png"}, scope="forsen")}
    }
    registry._global = {
        "bttv": {"Pog": Emote("g1", "Pog", "bttv", {"1x": "https://x/g.png"})}
    }
    assert registry.lookup("Pog").id == "c1"


def test_registry_twitch_emotes_scoped_queries():
    registry = EmoteRegistry(Settings())
    registry.set_twitch_emotes("forsen", {"25": "Kappa"})  # channel set (Kappa appears as a sub emote)
    registry.set_twitch_global_emotes({"1": "BibleThump"})
    assert registry.name_for_twitch_id("25") == "Kappa"
    assert registry.lookup("Kappa") is None  # Twitch emotes matched by IRC tags, not tokens
    subs = {e.name for e in registry.sub_emotes()}
    assert subs == {"Kappa"}
    globals_ = {e.name for e in registry.global_emotes()}
    assert globals_ == {"BibleThump"}
    all_names = {e.name for e in registry.all_emotes()}
    assert {"Kappa", "BibleThump"} <= all_names
    # The global Twitch emote carries no scope; channel ones carry the channel.
    assert all(e.scope is None for e in registry.global_emotes())
    assert registry._twitch_channel["Kappa"].scope == "forsen"


def test_registry_lookup_by_name_all_providers():
    registry = EmoteRegistry(Settings())
    registry.set_twitch_emotes("forsen", {"25": "Kappa"})  # channel (sub) set
    registry.set_twitch_global_emotes({"1": "BibleThump"})
    registry._channel = {"7tv": {"KEKW": Emote("a", "KEKW", "7tv", {"1x": "https://x/a.png"})}}
    assert registry.lookup_by_name("BibleThump").id == "1"
    assert registry.lookup_by_name("Kappa").id == "25"
    assert registry.lookup_by_name("KEKW").provider == "7tv"
    assert registry.lookup_by_name("nope") is None
    # The IRC-tag path stays third-party-only (tags are authoritative there).
    assert registry.lookup("Kappa") is None


def test_registry_lookup_by_name_twitch_channel_shadows_global():
    registry = EmoteRegistry(Settings())
    registry.set_twitch_global_emotes({"1": "Kappa"})
    registry.set_twitch_emotes("forsen", {"25": "Kappa"})
    assert registry.lookup_by_name("Kappa").id == "25"


def test_registry_split_emote_text():
    registry = EmoteRegistry(Settings())
    registry.set_twitch_global_emotes({"1": "Kappa", "2": "LUL"})
    registry._channel = {"7tv": {"KEKW": Emote("a", "KEKW", "7tv", {"1x": "https://x/a.png"})}}

    def names(pieces):
        return [(text, emote.name if emote else None) for text, emote in pieces]

    assert names(registry.split_emote_text("Kappa KEKW")) == [
        ("Kappa", "Kappa"), (" ", None), ("KEKW", "KEKW")]
    # Trailing punctuation detaches from the emote as its own text piece.
    assert names(registry.split_emote_text("Kappa, KEKW")) == [
        ("Kappa", "Kappa"), (",", None), (" ", None), ("KEKW", "KEKW")]
    # Adjacent emote names stay text — Twitch requires whitespace between
    # emotes (verified live on twitch.tv), so the optimistic row must match.
    assert names(registry.split_emote_text("KappaKappa")) == [("KappaKappa", None)]
    # A word that merely starts with an emote name stays text (whole-word match).
    assert names(registry.split_emote_text("hello KappaX")) == [
        ("hello", None), (" ", None), ("KappaX", None)]
    # Interior punctuation still separates emotes (`hey,Kappa` renders Kappa).
    assert names(registry.split_emote_text("hey,Kappa")) == [
        ("hey", None), (",", None), ("Kappa", "Kappa")]
    assert names(registry.split_emote_text("hi")) == [("hi", None)]


def test_registry_per_channel_twitch_emotes_independent():
    """Parallel Twitch tabs each keep their own channel emotes: joining a
    second channel no longer overwrites the first one's sets."""
    registry = EmoteRegistry(Settings())
    registry.set_twitch_emotes("#Alice", {"25": "Kappa"})
    registry.set_twitch_emotes("bob", {"25": "Dedhed"})
    # Scoped lookups resolve against the asked-for channel (names normalize).
    assert registry.lookup_by_name("Kappa", "alice").id == "25"
    assert registry.lookup_by_name("Kappa", "bob") is None
    assert registry.lookup_by_name("Dedhed", "bob").id == "25"
    assert registry.lookup_by_name("Dedhed", "alice") is None
    # IRC emote tags resolve per channel too (same id, different names).
    assert registry.name_for_twitch_id("25", "alice") == "Kappa"
    assert registry.name_for_twitch_id("25", "bob") == "Dedhed"
    # The active channel (picker view) is the last one loaded.
    assert registry.sub_emotes()[0].name == "Dedhed"


def test_registry_channel_lookup_falls_back_to_global():
    """A channel's scoped map wins; other channels fall back to the global set."""
    registry = EmoteRegistry(Settings())
    registry.set_twitch_global_emotes({"g": "LUL"})
    registry._channels["alice"] = {
        "7tv": {"KEKW": Emote("a", "KEKW", "7tv", {"1x": "https://x/a.png"})}
    }
    registry._global["7tv"] = {"KEKW": Emote("g", "KEKW", "7tv", {"1x": "https://x/g.png"})}
    assert registry.lookup("KEKW", "alice").id == "a"
    assert registry.lookup("KEKW", "bob").id == "g"
    assert registry.lookup_by_name("LUL", "alice").id == "g"


def test_registry_split_emote_text_is_channel_aware():
    """Optimistic sends scope to their own channel: a channel emote of one
    tab must not render as an emote in another tab."""
    registry = EmoteRegistry(Settings())
    registry.set_twitch_emotes("alice", {"25": "Dedhed"})

    def names(pieces):
        return [(text, emote.name if emote else None) for text, emote in pieces]

    text = "DedhedDedhed Dedhed"
    # Adjacent names stay text (Twitch parity); only the whole token matches.
    assert names(registry.split_emote_text(text, channel="alice")) == [
        ("DedhedDedhed", None), (" ", None), ("Dedhed", "Dedhed")]
    assert names(registry.split_emote_text(text, channel="bob")) == [
        ("DedhedDedhed", None), (" ", None), ("Dedhed", None)]
    # Channel-less splits use the active (last-loaded) channel.
    assert names(registry.split_emote_text("Dedhed")) == [("Dedhed", "Dedhed")]


def test_registry_space_twitch_emotes():
    """Outbound Twitch sends insert spaces between adjacent emote names so
    twitch.tv's whole-word parser renders them."""
    registry = EmoteRegistry(Settings())
    registry.set_twitch_global_emotes({"1": "Kappa"})
    registry.set_twitch_emotes("alice", {"25": "Dedhed"})

    assert registry.space_twitch_emotes("KappaKappa") == "Kappa Kappa"
    assert registry.space_twitch_emotes("Kappa") == "Kappa"
    # Mixed words stay untouched — respacing them would split a word Twitch
    # will show as text either way.
    assert registry.space_twitch_emotes("KappaX") == "KappaX"
    # Punctuation is preserved around respaced runs.
    assert registry.space_twitch_emotes("Kappa, KappaKappa") == "Kappa, Kappa Kappa"
    assert registry.space_twitch_emotes("KappaKappa,") == "Kappa Kappa,"
    # Channel-scoped names respace only in their own channel.
    assert registry.space_twitch_emotes("DedhedDedhed", channel="alice") == "Dedhed Dedhed"
    assert registry.space_twitch_emotes("DedhedDedhed", channel="bob") == "DedhedDedhed"
    # Third-party names never respace — twitch.tv does not render them.
    registry._channels["alice"] = {"7tv": {"KEKW": Emote("a", "KEKW", "7tv")}}
    assert registry.space_twitch_emotes("KEKWKEKW", channel="alice") == "KEKWKEKW"
    # Disabled Twitch provider: leave the text alone.
    off = EmoteRegistry(Settings({"emote_providers": {"twitch": False}}))
    off.set_twitch_global_emotes({"1": "Kappa"})
    assert off.space_twitch_emotes("KappaKappa") == "KappaKappa"


def test_registry_forget_channel_keeps_others():
    registry = EmoteRegistry(Settings())
    registry.set_twitch_global_emotes({"g": "LUL"})
    registry.set_twitch_emotes("alice", {"1": "Kappa"})
    registry.set_twitch_emotes("bob", {"2": "Dedhed"})
    registry.forget_channel("alice")
    assert registry.lookup_by_name("Kappa", "alice") is None
    assert registry.lookup_by_name("Dedhed", "bob").id == "2"
    assert registry.lookup_by_name("LUL", "bob").id == "g"  # globals untouched
    # Forgetting the active channel clears the picker views.
    registry.forget_channel("bob")
    assert registry.sub_emotes() == []


@respx.mock
async def test_registry_load_channel_separates_scopes():
    respx.get("https://7tv.io/v3/users/twitch/123").mock(
        return_value=httpx.Response(200, json={"emote_set": {"id": "set-1"}})
    )
    respx.get("https://7tv.io/v3/emote-sets/set-1").mock(
        return_value=httpx.Response(200, json={"emotes": [{"id": "a1", "name": "KEKW", "flags": 0, "data": {"animated": False, "host": {"url": "//cdn.7tv.app", "files": [{"name": "1x.webp"}]}}}]})
    )
    global_7tv = respx.get("https://7tv.io/v3/emote-sets/global").mock(
        return_value=httpx.Response(200, json={"emotes": []})
    )
    respx.get("https://api.betterttv.net/3/cached/users/twitch/123").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.betterttv.net/3/cached/emotes/global").mock(
        return_value=httpx.Response(200, json=[{"id": "g1", "code": "LULW", "imageType": "png"}])
    )
    respx.get("https://api.frankerfacez.com/v1/room/forsen").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://api.frankerfacez.com/v1/set/global").mock(
        return_value=httpx.Response(200, json={"set": {"id": 3, "emoticons": []}})
    )
    registry = EmoteRegistry(Settings())
    try:
        await registry.load_channel("forsen", "123")
        assert registry.lookup("KEKW").provider == "7tv"
        assert registry.lookup("LULW").provider == "bttv"
        assert registry._loaded_channel == "forsen"
        channel_names = {e.name for e in registry.channel_emotes()}
        assert channel_names == {"KEKW"}
        global_names = {e.name for e in registry.global_emotes()}
        assert global_names == {"LULW"}
        await registry.load_channel("forsen", "123")  # second load: globals cached
        assert global_7tv.call_count == 1
        registry.clear_channel()
        assert registry.lookup("KEKW") is None and registry.lookup("LULW") is None
        assert registry.channel_emotes() == [] and registry.global_emotes() == []
    finally:
        await registry.close()
