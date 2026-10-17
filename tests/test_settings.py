"""Settings: M4 defaults, deep-merge of old files, set_batch persistence."""

from chattersyn.config.settings import DEFAULTS, Settings


def test_m4_defaults_present():
    assert DEFAULTS["highlight_color"] == "#7a5cff"
    assert DEFAULTS["favourites"] == {"emotes": [], "emojis": []}
    assert DEFAULTS["autocomplete"] == {"emotes": True, "users": True, "commands": True}
    assert DEFAULTS["logging"]["path"] == ""


def test_deep_merge_supplies_new_defaults_to_old_files():
    settings = Settings({"logging": {"enabled": False}, "theme": "light"})
    assert settings.get("logging.enabled") is False  # old value kept
    assert settings.get("logging.path") == ""  # new default supplied
    assert settings.get("logging.format") == "[HH:mm:ss] {name}: {msg}"
    assert settings.get("highlight_color") == "#7a5cff"
    assert settings.get("autocomplete.emotes") is True


def test_set_batch_persists_and_emits_once_per_key(tmp_path):
    settings = Settings(path=tmp_path / "settings.json")
    events = []
    settings.changed.connect(lambda: events.append(1))
    settings.set_batch({
        "highlight_color": "#ff0000",
        "logging.path": str(tmp_path / "logs"),
    })
    assert len(events) == 2
    reloaded = Settings.load(tmp_path)
    assert reloaded.get("highlight_color") == "#ff0000"
    assert reloaded.get("logging.path") == str(tmp_path / "logs")
    # unrelated defaults survived the round-trip
    assert reloaded.get("logging.enabled") is True
