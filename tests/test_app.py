"""App bootstrap helpers: config migration from the pre-rename org dir."""

from chattersyn.app import _migrate_legacy_config


def test_migrate_copies_settings_and_secrets(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    (old / "settings.json").write_text('{"tabs": []}')
    (old / "secrets.json").write_text("{}")

    _migrate_legacy_config(old, new)

    assert (new / "settings.json").read_text() == '{"tabs": []}'
    assert (new / "secrets.json").read_text() == "{}"


def test_migrate_skips_when_new_dir_already_exists(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "settings.json").write_text('{"tabs": []}')
    (new / "settings.json").write_text('{"tabs": [{"twitch_channel": "xqc"}]}')

    _migrate_legacy_config(old, new)

    assert (new / "settings.json").read_text() == '{"tabs": [{"twitch_channel": "xqc"}]}'


def test_migrate_noop_without_legacy_dir(tmp_path):
    _migrate_legacy_config(tmp_path / "missing", tmp_path / "new")
    assert not (tmp_path / "new").exists()
