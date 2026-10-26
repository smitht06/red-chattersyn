"""DiskCache: downloaded image bytes kept on disk between runs, so emotes,
badges and avatars show instantly on the next start instead of being
re-downloaded. One file per URL (named by its sha256), size-capped with
oldest-first pruning, entries expire after `max_age`. Every method is
synchronous and meant for an executor thread; failures are the caller's to
swallow (the network path must always keep working)."""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import time
from pathlib import Path

MAX_BYTES = 256 * 1024 * 1024
MAX_AGE = 30 * 24 * 3600  # emote/badge/avatar URLs are immutable by id


class DiskCache:
    def __init__(self, root: Path, *, max_bytes: int = MAX_BYTES, max_age: float = MAX_AGE):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.max_age = max_age

    def _path(self, url: str) -> Path:
        return self.root / hashlib.sha256(url.encode("utf-8")).hexdigest()

    def get(self, url: str) -> bytes | None:
        path = self._path(url)
        try:
            if time.time() - path.stat().st_mtime > self.max_age:
                return None
            data = path.read_bytes()
            os.utime(path)  # recently used: keep it through the next prune
            return data
        except FileNotFoundError:
            return None

    def put(self, url: str, data: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, self._path(url))
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def _entries(self) -> list[tuple[float, int, Path]]:
        if not self.root.is_dir():
            return []
        entries = []
        for path in self.root.iterdir():
            with contextlib.suppress(FileNotFoundError):
                stat = path.stat()
                entries.append((stat.st_mtime, stat.st_size, path))
        return entries

    def prune(self) -> int:
        """Drop expired files, then the oldest until under max_bytes; returns
        how many were removed."""
        now = time.time()
        removed = 0
        keep = []
        for mtime, size, path in sorted(self._entries()):
            # A .tmp- file under an hour old may be a write still in flight.
            stale_tmp = path.name.startswith(".tmp-") and now - mtime > 3600
            if now - mtime > self.max_age or stale_tmp:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                    removed += 1
            else:
                keep.append((mtime, size, path))
        total = sum(size for _, size, _ in keep)
        for _, size, path in keep:  # oldest first
            if total <= self.max_bytes:
                break
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
                removed += 1
            total -= size
        return removed

    def clear(self) -> None:
        for _, _, path in self._entries():
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def size_bytes(self) -> int:
        return sum(size for _, size, _ in self._entries())

