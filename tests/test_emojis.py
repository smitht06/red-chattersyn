"""Bundled emoji dataset: shape, categories, well-known entries, caching."""

from chattersyn.services.emojis import CATEGORY_ORDER, emoji_dataset


def test_dataset_loads():
    assert len(emoji_dataset()) > 1000


def test_well_known_shortcodes_present():
    codes = {e.shortcode for e in emoji_dataset()}
    assert {"face_with_tears_of_joy", "grinning_face", "thumbs_up", "red_heart"} <= codes


def test_all_categories_known_and_used():
    cats = {e.category for e in emoji_dataset()}
    assert cats == set(CATEGORY_ORDER)  # every picker tab group has rows


def test_entries_well_formed():
    for entry in emoji_dataset():
        assert entry.shortcode and entry.char


def test_dataset_cached():
    assert emoji_dataset() is emoji_dataset()
