"""SecretsStore: keyring first, an owner-only file fallback, a one-time warning."""

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
