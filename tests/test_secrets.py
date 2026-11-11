"""SecretsStore: keyring first, an owner-only file fallback, a one-time warning."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from chattersyn.config.secrets import SecretsStore


class _BrokenKeyring:
    def get_password(self, service, key):
        raise RuntimeError("no keyring backend")

    def set_password(self, service, key, value):
        raise RuntimeError("no keyring backend")

    def delete_password(self, service, key):
        raise RuntimeError("no keyring backend")


class _MemoryKeyring:
    def __init__(self):
        self.data = {}

    def get_password(self, service, key):
        return self.data.get((service, key))

    def set_password(self, service, key, value):
        self.data[(service, key)] = value

    def delete_password(self, service, key):
        self.data.pop((service, key), None)


def _store(tmp_path, keyring, reports):
    store = SecretsStore(tmp_path, on_fallback=reports.append)
    store._keyring = keyring
    return store


def test_working_keyring_never_writes_the_file(tmp_path):
    reports = []
    store = _store(tmp_path, _MemoryKeyring(), reports)
    store.set("twitch_tokens", "abc")
    assert store.get("twitch_tokens") == "abc"
    assert not (tmp_path / "secrets.json").exists()
    assert reports == []


@pytest.mark.parametrize("keyring", [None, _BrokenKeyring()], ids=["no-keyring", "keyring-error"])
def test_fallback_file_is_used_and_reported_once(tmp_path, keyring):
    reports = []
    store = _store(tmp_path, keyring, reports)
    store.set("twitch_tokens", "abc")
    store.set("youtube_tokens", "def")
    assert store.get("twitch_tokens") == "abc"
    assert reports == [tmp_path / "secrets.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_fallback_file_is_never_readable_by_others(tmp_path, monkeypatch):
    leftover = tmp_path / "secrets.json.tmp"  # an older build's interrupted write
    leftover.write_text("{}")
    os.chmod(leftover, 0o644)
    modes = []
    real_replace = Path.replace

    def spy(self, target):
        modes.append(stat.S_IMODE(self.stat().st_mode))  # just before the rename
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    _store(tmp_path, None, []).set("twitch_tokens", "abc")
    assert modes == [0o600]
    assert stat.S_IMODE((tmp_path / "secrets.json").stat().st_mode) == 0o600


# -- moving a leftover secrets.json into the keyring (LD-5, LD-6) --------------------------


class _RefusingKeyring(_MemoryKeyring):
    """Stores everything except one key."""

    def __init__(self, refuse):
        super().__init__()
        self.refuse = refuse

    def set_password(self, service, key, value):
        if key == self.refuse:
            raise RuntimeError("keyring refused")
        super().set_password(service, key, value)


class _ForgetfulKeyring(_MemoryKeyring):
    """Accepts writes but reads back nothing (a broken backend)."""

    def get_password(self, service, key):
        return None


def _plaintext(tmp_path, data):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_file_moved_into_working_keyring(tmp_path):
    """LD-5."""
    path = _plaintext(tmp_path, {"twitch_token": "tok", "youtube_api_key": "key"})
    keyring = _MemoryKeyring()
    store = _store(tmp_path, keyring, [])
    assert store.migrate_file_to_keyring() is True
    assert not path.exists()
    assert store.get("twitch_token") == "tok"
    assert store.get("youtube_api_key") == "key"


def test_refused_entry_keeps_file(tmp_path):
    """LD-6: the file stays byte-identical."""
    path = _plaintext(tmp_path, {"twitch_token": "tok", "youtube_api_key": "key"})
    before = path.read_bytes()
    store = _store(tmp_path, _RefusingKeyring("youtube_api_key"), [])
    assert store.migrate_file_to_keyring() is False
    assert path.read_bytes() == before


def test_migration_verifies_by_reading_back(tmp_path):
    """Review Focus 2: a keyring that accepts but can't read back keeps the file."""
    path = _plaintext(tmp_path, {"twitch_token": "tok"})
    store = _store(tmp_path, _ForgetfulKeyring(), [])
    assert store.migrate_file_to_keyring() is False
    assert path.exists()


def test_no_file_no_change(tmp_path):
    store = _store(tmp_path, _MemoryKeyring(), [])
    assert store.migrate_file_to_keyring() is False
    assert not (tmp_path / "secrets.json").exists()


def test_no_keyring_keeps_file(tmp_path):
    path = _plaintext(tmp_path, {"twitch_token": "tok"})
    store = _store(tmp_path, None, [])
    assert store.migrate_file_to_keyring() is False
    assert path.exists()


def test_keyring_write_clears_stale_file_entry(tmp_path):
    """Final review #2: once a key's write reaches the keyring, the file's older
    copy is dropped, so a later migration can't restore a stale token."""
    path = _plaintext(tmp_path, {"twitch_token": "old", "youtube_api_key": "key"})
    keyring = _MemoryKeyring()
    store = _store(tmp_path, keyring, [])
    store.set("twitch_token", "new")
    assert json.loads(path.read_text(encoding="utf-8")) == {"youtube_api_key": "key"}
    assert store.migrate_file_to_keyring() is True
    assert store.get("twitch_token") == "new"


def test_unreadable_file_is_not_deleted(tmp_path):
    """Final review #8: a secrets.json that can't be parsed is left alone."""
    path = tmp_path / "secrets.json"
    path.write_text("{not json", encoding="utf-8")
    store = _store(tmp_path, _MemoryKeyring(), [])
    assert store.migrate_file_to_keyring() is False
    assert path.exists()
