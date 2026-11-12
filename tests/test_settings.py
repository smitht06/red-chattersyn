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


# -- backup and recovery (LD-1..LD-4) ------------------------------------------------------

import datetime as dt  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

from chattersyn.config import settings as settings_mod  # noqa: E402

RESTORED = "Your settings file was damaged — restored the last good copy."
NO_BACKUP = ("Your settings file was damaged and no backup could be read — "
             "started with default settings.")
FIXED_NOW = dt.datetime(2026, 11, 11, 9, 30, 15)


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_save_keeps_previous_as_bak(tmp_path):
    """LD-1."""
    _write(tmp_path / "settings.json", {"tabs": [{"twitch_channel": "old"}]})
    s = Settings.load(tmp_path)
    s.set("tabs", [{"twitch_channel": "new"}])
    s.save()
    bak = json.loads((tmp_path / "settings.json.bak").read_text(encoding="utf-8"))
    assert bak["tabs"] == [{"twitch_channel": "old"}]
    now = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert now["tabs"] == [{"twitch_channel": "new"}]


def test_corrupt_restored_from_bak(tmp_path):
    """LD-2."""
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path / "settings.json.bak", {"tabs": [{"twitch_channel": "kept"}]})
    s = Settings.load(tmp_path, now=lambda: FIXED_NOW)
    assert s.get("tabs") == [{"twitch_channel": "kept"}]
    assert s.load_problem == RESTORED
    corrupt = tmp_path / "settings.json.corrupt-20261111-093015"
    assert corrupt.read_text(encoding="utf-8") == "{not json"
    assert not (tmp_path / "settings.json").exists()


def test_corrupt_without_bak_uses_defaults(tmp_path):
    """LD-3."""
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    s = Settings.load(tmp_path, now=lambda: FIXED_NOW)
    assert s.load_problem == NO_BACKUP
    assert s.get("tabs") == DEFAULTS.get("tabs", s.get("tabs"))
    assert (tmp_path / "settings.json.corrupt-20261111-093015").exists()


def test_corrupt_with_damaged_bak_uses_defaults(tmp_path):
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "settings.json.bak").write_text("also bad", encoding="utf-8")
    s = Settings.load(tmp_path, now=lambda: FIXED_NOW)
    assert s.load_problem == NO_BACKUP


def test_first_launch_no_message(tmp_path):
    """LD-4."""
    s = Settings.load(tmp_path)
    assert s.load_problem == ""


def test_good_file_no_message(tmp_path):
    _write(tmp_path / "settings.json", {"tabs": []})
    assert Settings.load(tmp_path).load_problem == ""


def test_bak_written_before_replace(tmp_path, monkeypatch):
    """Review Focus 1: a save that fails at the replace leaves settings.json as
    it was, with the .bak already written."""
    _write(tmp_path / "settings.json", {"tabs": [{"twitch_channel": "old"}]})
    s = Settings.load(tmp_path)
    s.set("tabs", [{"twitch_channel": "new"}])

    real_replace = settings_mod.Path.replace

    def boom(self, target):
        if str(target).endswith("settings.json"):
            raise OSError("disk full")
        return real_replace(self, target)
    monkeypatch.setattr(settings_mod.Path, "replace", boom)
    with pytest.raises(OSError):
        s.save()
    monkeypatch.undo()
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["tabs"] == [
        {"twitch_channel": "old"}]
    assert (tmp_path / "settings.json.bak").exists()


def test_damaged_file_is_not_copied_over_a_good_bak(tmp_path):
    """Saving after a recovery must not turn the good .bak into a copy of nothing."""
    _write(tmp_path / "settings.json.bak", {"tabs": [{"twitch_channel": "kept"}]})
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    s = Settings.load(tmp_path, now=lambda: FIXED_NOW)
    s.save()
    bak = json.loads((tmp_path / "settings.json.bak").read_text(encoding="utf-8"))
    assert bak["tabs"] == [{"twitch_channel": "kept"}]


def test_failed_rename_still_never_overwrites_damaged_file(tmp_path, monkeypatch):
    """Final review #5: when setting the damaged file aside by rename fails, it's
    copied aside instead, and the next save can't destroy the only copy."""
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    real_replace = settings_mod.Path.replace

    def no_rename(self, target):
        if "corrupt" in str(target):
            raise OSError("file in use")
        return real_replace(self, target)
    monkeypatch.setattr(settings_mod.Path, "replace", no_rename)
    s = Settings.load(tmp_path, now=lambda: FIXED_NOW)
    assert (tmp_path / "settings.json.corrupt-20261111-093015").read_text(
        encoding="utf-8") == "{not json"
    s.save()


def test_backup_failure_does_not_block_saving(tmp_path, monkeypatch):
    """Final review #6: a .bak that can't be written logs, and the save goes on."""
    _write(tmp_path / "settings.json", {"tabs": []})
    s = Settings.load(tmp_path)
    s.set("tabs", [{"twitch_channel": "new"}])

    def boom(*args, **kwargs):
        raise OSError("read-only")
    monkeypatch.setattr(settings_mod.shutil, "copyfile", boom)
    s.save()
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["tabs"] == [
        {"twitch_channel": "new"}]


def test_same_second_corrupt_names_do_not_collide(tmp_path):
    """Final review #7."""
    for _ in range(2):
        (tmp_path / "settings.json").write_text("{bad", encoding="utf-8")
        Settings.load(tmp_path, now=lambda: FIXED_NOW)
    names = sorted(p.name for p in tmp_path.glob("settings.json.corrupt-*"))
    assert len(names) == 2



def test_browser_source_defaults():
    """OBS-1 / OBS-15 defaults."""
    s = Settings({})
    assert s.get("browser_source") == {
        "enabled": False, "port": 17830, "font_family": "", "font_size": 20,
        "text_color": "#ffffff", "background": "#00000000",
        "background_fills_source": True, "outline": True,
        "show_platform_icons": True, "show_badges": True, "show_emotes": True,
        "show_timestamps": False, "fade_seconds": 30, "max_messages": 50,
    }
