"""EmotePickerPopup: tab structure/default, per-tab scopes, search mode,
click-to-insert, Ctrl/Ctrl+Shift favourites with persistence, unresolved
favourites notice and emoji category headers."""

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from chattersyn.config.settings import Settings
from chattersyn.models.emote import Emote
from chattersyn.services.emojis import EmojiEntry
from chattersyn.services.emotes.emote_registry import EmoteRegistry
from chattersyn.ui.emote_picker import TABS, EmotePickerPopup

_KIND_EMOTE = "emote"
_KIND_EMOJI = "emoji"


def _dataset():
    return (
        EmojiEntry("joy", "😂", "Smileys & Emotion"),
        EmojiEntry("thumbs_up", "👍", "People & Body"),
        EmojiEntry("red_hair", "🦰", "Component"),
    )


@pytest.fixture
def registry(tmp_path):
    settings = Settings(path=tmp_path / "settings.json")
    reg = EmoteRegistry(settings)
    reg.set_twitch_emotes("forsen", {"1": "PogChamp"})
    reg.set_twitch_global_emotes({"2": "LULW"})
    reg._channel["7tv"] = {"KEKW": Emote(id="k1", name="KEKW", provider="7tv")}
    reg._global["7tv"] = {"widepepoHappy": Emote(id="k2", name="widepepoHappy", provider="7tv")}
    return settings, reg


@pytest.fixture
def picker(qapp, monkeypatch, registry):
    settings, reg = registry
    monkeypatch.setattr("chattersyn.ui.emote_picker.emoji_dataset", _dataset)
    return EmotePickerPopup(settings, reg)


def _texts(popup):
    return [popup._list.item(i).text() for i in range(popup._list.count())]


def _headers(popup):
    return [t for i, t in enumerate(_texts(popup))
            if not bool(popup._list.item(i).flags())]


def _item(popup, text):
    for i in range(popup._list.count()):
        if popup._list.item(i).text() == text:
            return popup._list.item(i)
    raise AssertionError(f"no list item {text!r}")


def _insert_spy(popup):
    inserted = []
    popup.insert_requested.connect(inserted.append)
    return inserted


# -- tabs ---------------------------------------------------------------------------------


def test_tab_order_and_default(qapp, monkeypatch, registry):
    settings, reg = registry
    assert TABS == ("Favourite", "Subs", "Channel", "Global", "Emojis")
    popup = EmotePickerPopup(settings, reg)
    assert TABS[popup._tabs.currentIndex()] == "Subs"


def test_default_tab_emojis_for_youtube_only(qapp, monkeypatch, registry):
    settings, reg = registry
    monkeypatch.setattr("chattersyn.ui.emote_picker.emoji_dataset", _dataset)
    popup = EmotePickerPopup(settings, reg, default_tab="Emojis")
    assert TABS[popup._tabs.currentIndex()] == "Emojis"


def test_tab_scopes(picker):
    assert "PogChamp" in _texts(picker)          # Subs default
    assert "LULW" not in _texts(picker)
    picker._tabs.setCurrentIndex(2)              # Channel
    assert _texts(picker) == ["KEKW"]
    picker._tabs.setCurrentIndex(3)              # Global
    texts = _texts(picker)
    assert "LULW" in texts and "widepepoHappy" in texts


def test_emojis_tab_category_headers(picker):
    picker._tabs.setCurrentIndex(4)
    assert _headers(picker) == ["Smileys & Emotion", "People & Body", "Component"]


# -- search -----------------------------------------------------------------------------


def test_search_hides_tabs_and_filters(picker):
    picker._search.setText("kek")
    assert picker._tabs.isHidden()
    assert _texts(picker) == ["KEKW"]
    picker._search.setText("")
    assert not picker._tabs.isHidden()
    assert "PogChamp" in _texts(picker)  # back to the current tab's scope


def test_search_matches_emoji_shortcodes(picker):
    picker._search.setText("joy")
    assert any(":joy:" in text for text in _texts(picker))


# -- click insertion ---------------------------------------------------------------------


def test_click_inserts_emote_name(picker):
    picker._search.setText("kek")
    inserted = _insert_spy(picker)
    picker._on_item_left_clicked(_item(picker, "KEKW"))
    assert inserted == ["KEKW"]


def test_emoji_click_inserts_shortcode(picker):
    picker._search.setText("joy")
    item = next(picker._list.item(i) for i in range(picker._list.count())
                if ":joy:" in picker._list.item(i).text())
    inserted = _insert_spy(picker)
    picker._on_item_left_clicked(item)
    assert inserted == [":joy:"]


# -- favourites ---------------------------------------------------------------------------


class _Mods:
    value = Qt.KeyboardModifier.NoModifier

    @staticmethod
    def keyboardModifiers():
        return _Mods.value


def test_ctrl_click_toggles_and_persists_favourite(qapp, monkeypatch, registry):
    settings, reg = registry
    monkeypatch.setattr("chattersyn.ui.emote_picker.emoji_dataset", _dataset)
    monkeypatch.setattr("chattersyn.ui.emote_picker.QGuiApplication", _Mods)
    _Mods.value = Qt.KeyboardModifier.ControlModifier
    popup = EmotePickerPopup(settings, reg)
    popup._search.setText("kek")
    item = _item(popup, "KEKW")

    inserted = _insert_spy(popup)
    popup._on_item_left_clicked(item)
    assert inserted == []  # ctrl = toggle only
    assert settings.get("favourites") == {"emotes": ["KEKW"], "emojis": []}
    saved = json.loads((settings.path).read_text())
    assert saved["favourites"]["emotes"] == ["KEKW"]

    popup._on_item_left_clicked(item)  # toggle off
    assert settings.get("favourites") == {"emotes": [], "emojis": []}


def test_ctrl_shift_click_toggles_and_inserts(qapp, monkeypatch, registry):
    settings, reg = registry
    monkeypatch.setattr("chattersyn.ui.emote_picker.emoji_dataset", _dataset)
    monkeypatch.setattr("chattersyn.ui.emote_picker.QGuiApplication", _Mods)
    _Mods.value = (Qt.KeyboardModifier.ControlModifier
                   | Qt.KeyboardModifier.ShiftModifier)
    popup = EmotePickerPopup(settings, reg)
    popup._search.setText("kek")
    inserted = _insert_spy(popup)
    popup._on_item_left_clicked(_item(popup, "KEKW"))
    assert inserted == ["KEKW"]
    assert settings.get("favourites")["emotes"] == ["KEKW"]


def test_emoji_favourites_use_emojis_bucket(qapp, monkeypatch, registry):
    settings, reg = registry
    monkeypatch.setattr("chattersyn.ui.emote_picker.emoji_dataset", _dataset)
    monkeypatch.setattr("chattersyn.ui.emote_picker.QGuiApplication", _Mods)
    _Mods.value = Qt.KeyboardModifier.ControlModifier
    popup = EmotePickerPopup(settings, reg)
    popup._search.setText("joy")
    item = next(popup._list.item(i) for i in range(popup._list.count())
                if ":joy:" in popup._list.item(i).text())
    popup._on_item_left_clicked(item)
    assert settings.get("favourites") == {"emotes": [], "emojis": ["joy"]}


def test_favourite_tab_resolves_registry_emotes(qapp, monkeypatch, registry):
    settings, reg = registry
    settings.set("favourites", {"emotes": ["kekw"], "emojis": ["joy"]})
    monkeypatch.setattr("chattersyn.ui.emote_picker.emoji_dataset", _dataset)
    popup = EmotePickerPopup(settings, reg, default_tab="Favourite")
    assert _headers(popup) == ["Favourite emotes", "Favourite emojis"]
    texts = _texts(popup)
    assert "kekw" in texts and any(":joy:" in t for t in texts)


def test_unresolved_favourites_notice(qapp, monkeypatch, registry):
    settings, reg = registry
    settings.set("favourites", {"emotes": ["NOPE"], "emojis": []})
    monkeypatch.setattr("chattersyn.ui.emote_picker.emoji_dataset", _dataset)
    popup = EmotePickerPopup(settings, reg, default_tab="Favourite")
    assert "Unresolved favourites (provider not loaded)" in _headers(popup)
    item = _item(popup, "NOPE")
    assert not bool(item.flags())  # disabled gray row


def test_empty_favourites_hint(qapp, monkeypatch, registry):
    settings, reg = registry
    monkeypatch.setattr("chattersyn.ui.emote_picker.emoji_dataset", _dataset)
    popup = EmotePickerPopup(settings, reg, default_tab="Favourite")
    assert _headers(popup) == ["Nothing favourited yet — Ctrl+click an emote to add it"]


def test_legacy_favourites_list_shape(qapp, monkeypatch, registry):
    settings, reg = registry
    settings.set("favourites", ["KEKW"])  # pre-dict shape = emotes
    monkeypatch.setattr("chattersyn.ui.emote_picker.emoji_dataset", _dataset)
    popup = EmotePickerPopup(settings, reg, default_tab="Favourite")
    assert _item(popup, "KEKW")


# -- keyboard ------------------------------------------------------------------------------


def test_escape_closes(qapp, monkeypatch, registry):
    settings, reg = registry
    monkeypatch.setattr("chattersyn.ui.emote_picker.emoji_dataset", _dataset)
    popup = EmotePickerPopup(settings, reg)
    closed = []
    popup.closed.connect(lambda: closed.append(True))
    QTest.keyClick(popup, Qt.Key.Key_Escape)
    assert closed == [True]


def test_ctrl_tab_cycles_tabs(picker):
    start = picker._tabs.currentIndex()
    QTest.keyClick(picker, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
    assert picker._tabs.currentIndex() == (start + 1) % len(TABS)
