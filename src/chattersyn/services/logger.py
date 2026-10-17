"""Per-channel chat logging: one file per channel per day under logs/.

Subscribes bridge.message; every line is best-effort — a full queue or a
broken file must never take the app down. Lines are buffered and flushed
every _FLUSH_INTERVAL seconds, so a crash loses at most that window.

File layout (Chatterino-style): <logs_root>/<channel_key>/<YYYY-MM-DD>.log
where channel_key is twitch-<channel> or youtube-<video_id> (the video id is
stable across streams; a live_chat_id is not).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import re
from pathlib import Path
from typing import TextIO

from ..config.paths import config_dir
from ..config.settings import Settings
from ..core.bus import bridge
from ..models.message import Message, Platform

log = logging.getLogger(__name__)

_FLUSH_INTERVAL = 2.0
_QUEUE_LIMIT = 4096

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize(key: str) -> str:
    return _SANITIZE_RE.sub("_", key).strip("_") or "unknown"


def log_channel_key(msg: Message) -> str:
    """Stable per-channel log key for a message."""
    if msg.platform is Platform.YOUTUBE:
        base = msg.extra.get("video_id") or msg.channel_id
        return f"youtube-{_sanitize(base)}"
    return f"twitch-{_sanitize(msg.channel_id)}"


def format_timestamp(ts: dt.datetime, fmt: str) -> str:
    """Substitute the delegate's timestamp tokens (HH, hh, mm, ss)."""
    out = fmt
    for token, value in (
        ("HH", f"{ts.hour:02d}"),
        ("hh", f"{ts.hour % 12 or 12:02d}"),
        ("mm", f"{ts.minute:02d}"),
        ("ss", f"{ts.second:02d}"),
    ):
        out = out.replace(token, value)
    return out


class ChatLogger:
    """Writes every bridge message to its channel's daily log file."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._queue: asyncio.Queue[Message | None] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._writer_task: asyncio.Task | None = None
        self._conn = None
        self._files: dict[Path, TextIO] = {}
        self._today = lambda: dt.datetime.now().strftime("%Y-%m-%d")

    @property
    def logs_root(self) -> Path:
        configured = str(self._settings.get("logging.path", "") or "").strip()
        if configured:
            return Path(configured)
        return config_dir() / "logs"

    def start(self) -> None:
        self._conn = bridge.message.connect(self._on_message)
        self._writer_task = asyncio.ensure_future(self._writer())

    async def stop(self) -> None:
        """Drain the queue (sentinel), then close all files."""
        if self._conn is not None:
            bridge.message.disconnect(self._conn)
            self._conn = None
        if self._writer_task is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
            await self._writer_task
            self._writer_task = None
        self._close_files()

    # -- ingress ---------------------------------------------------------------------

    def _on_message(self, msg: Message) -> None:
        if not bool(self._settings.get("logging.enabled", True)):
            return
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("chat log queue full — dropping a line")

    # -- writer ----------------------------------------------------------------------

    async def _writer(self) -> None:
        while True:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=_FLUSH_INTERVAL)
            except TimeoutError:
                self._flush()
                continue
            if msg is None:
                break
            line = self._format_line(msg)
            try:
                self._file_for(log_channel_key(msg)).write(line + "\n")
            except OSError as exc:
                log.warning("chat log write failed: %s", exc)

    def _file_for(self, key: str) -> TextIO:
        """Open handle for key+today, reusing it across messages."""
        path = self.logs_root / key / f"{self._today()}.log"
        fh = self._files.get(path)
        if fh is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = path.open("a", encoding="utf-8")
            self._files[path] = fh
        return fh

    def _flush(self) -> None:
        for fh in self._files.values():
            try:
                fh.flush()
            except OSError as exc:
                log.warning("chat log flush failed: %s", exc)

    def _close_files(self) -> None:
        for fh in self._files.values():
            with contextlib.suppress(OSError):
                fh.close()
        self._files.clear()

    # -- formatting ------------------------------------------------------------------

    def _format_line(self, msg: Message) -> str:
        fmt = str(self._settings.get("logging.format") or "[HH:mm:ss] {name}: {msg}")
        ts = msg.ts if isinstance(msg.ts, dt.datetime) else dt.datetime.fromtimestamp(msg.ts)
        body = f"*{msg.plain}*" if msg.is_system else msg.plain
        return (
            format_timestamp(ts, fmt)
            .replace("{name}", msg.author.name if msg.author else "")
            .replace("{msg}", body)
            .replace("{channel}", msg.channel_id)
            .replace("{platform}", msg.platform.value)
            .replace("{id}", msg.id)
        )
