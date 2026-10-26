"""DiskCache: on-disk image bytes between runs."""

import os
import time

from chattersyn.core.disk_cache import DiskCache


def test_put_get_round_trip_and_miss(tmp_path):
    cache = DiskCache(tmp_path / "images")
    assert cache.get("https://cdn.example/a.png") is None  # no dir yet: plain miss
    cache.put("https://cdn.example/a.png", b"png-bytes")
    assert cache.get("https://cdn.example/a.png") == b"png-bytes"
    assert cache.get("https://cdn.example/b.png") is None
    assert not [p for p in (tmp_path / "images").iterdir() if p.name.startswith(".tmp-")]


def test_expired_entries_miss_and_prune(tmp_path):
    cache = DiskCache(tmp_path, max_age=60)
    cache.put("u", b"x")
    old = time.time() - 120
    path = next(tmp_path.iterdir())
    os.utime(path, (old, old))
    assert cache.get("u") is None
    assert cache.prune() == 1
    assert list(tmp_path.iterdir()) == []


def test_prune_drops_oldest_until_under_cap(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=25)
    now = time.time()
    for i, url in enumerate(("old", "mid", "new")):
        cache.put(url, b"x" * 10)
        stamp = now - 100 + i
        os.utime(cache._path(url), (stamp, stamp))
    cache.get("old")  # a hit refreshes it: now the newest
    cache.prune()
    assert cache.get("mid") is None
    assert cache.get("old") == b"x" * 10 and cache.get("new") == b"x" * 10
    assert cache.size_bytes() == 20


def test_clear_empties(tmp_path):
    cache = DiskCache(tmp_path)
    cache.put("a", b"1")
    cache.put("b", b"2")
    cache.clear()
    assert cache.size_bytes() == 0 and cache.get("a") is None
