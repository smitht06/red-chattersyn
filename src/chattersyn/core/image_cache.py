"""Async image cache: fetch via httpx, decode QImage in an executor thread,
construct QPixmap/QMovie on the GUI thread (thread-affinity rule), LRU with
byte accounting and a cap on concurrent QMovies."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

import httpx
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Signal
from PySide6.QtGui import QImage, QImageReader, QMovie, QPixmap

from .sandbox_fix import unhide_venv_files

log = logging.getLogger(__name__)

MAX_BYTES = 256 * 1024 * 1024
MAX_MOVIES = 64
TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0)


def _decode_image(data: bytes) -> tuple[QImage, bool]:
    """Runs in an executor thread; QImage/QImageReader are safe off the GUI thread.
    Returns (first frame, has more than one frame)."""
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buf)
    multi_frame = reader.supportsAnimation() and reader.imageCount() > 1
    return reader.read(), multi_frame


def _estimate_bytes(image: QImage) -> int:
    return max(image.sizeInBytes(), image.width() * image.height() * 4)


class ImageCache(QObject):
    image_ready = Signal(str)  # url
    frame_changed = Signal(str)  # url — an animated emote advanced a frame

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._cache: OrderedDict[str, QPixmap | tuple[QMovie, QBuffer]] = OrderedDict()
        self._bytes = 0
        self._inflight: set[str] = set()
        self._movies = 0
        self._client: httpx.AsyncClient | None = None
        self.max_bytes = MAX_BYTES
        self.max_movies = MAX_MOVIES

    # -- public ---------------------------------------------------------------

    def request(self, url: str, animated: bool = False) -> QPixmap | QMovie | None:
        """Return the cached object for `url`, or None (a fetch is then in flight).

        With `animated`, multi-frame images come back as a playing QMovie and
        single-frame ones as a QPixmap. Call again after the `image_ready(url)`
        signal to get the image.
        """
        key = self._key(url, animated)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached[0] if isinstance(cached, tuple) else cached
        if key not in self._inflight:
            self._inflight.add(key)
            asyncio.ensure_future(self._fetch(url, animated, key))
        return None

    def invalidate(self, url: str) -> None:
        for animated in (False, True):
            key = self._key(url, animated)
            entry = self._cache.pop(key, None)
            if entry is not None:
                self._discard(entry)

    def clear(self) -> None:
        while self._cache:
            _, entry = self._cache.popitem(last=False)
            self._discard(entry)

    # -- internals --------------------------------------------------------------

    @staticmethod
    def _key(url: str, animated: bool) -> str:
        return ("a:" if animated else "i:") + url

    async def _fetch(self, url: str, animated: bool, key: str) -> None:
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=TIMEOUT)
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.content
        except Exception as exc:
            log.debug("image fetch failed for %s: %s", url, exc)
            self._inflight.discard(key)
            return

        # Imageformat plugins load lazily at first decode; the sandbox may have
        # re-hidden them since startup, so heal right before decoding.
        unhide_venv_files()
        loop = asyncio.get_running_loop()
        try:
            image, multi_frame = await loop.run_in_executor(None, _decode_image, data)
        except Exception as exc:
            log.debug("image decode failed for %s: %s", url, exc)
            self._inflight.discard(key)
            return

        if image.isNull():
            self._inflight.discard(key)
            return

        # An "animated" request is a request to play the image IF it has
        # frames: single-frame data is stored as a plain pixmap so it never
        # takes one of the max_movies slots (Twitch emotes are requested this
        # way because IRC tags don't say which ones animate).
        if animated and multi_frame:
            buffer = QBuffer()
            buffer.setData(QByteArray(data))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            movie = QMovie(buffer)
            movie.setCacheMode(QMovie.CacheMode.CacheAll)
            movie.frameChanged.connect(lambda _frame, u=url: self.frame_changed.emit(u))
            movie.start()
            entry: QPixmap | tuple[QMovie, QBuffer] = (movie, buffer)
            self._movies += 1
        else:
            entry = QPixmap.fromImage(image)

        self._cache[key] = entry
        self._cache.move_to_end(key)
        self._bytes += _estimate_bytes(image)
        self._evict()
        self.image_ready.emit(url)
        self._inflight.discard(key)

    def _discard(self, entry: QPixmap | tuple[QMovie, QBuffer]) -> None:
        if isinstance(entry, tuple):
            movie, _buffer = entry
            self._movies -= 1
            movie.stop()
            movie.setDevice(None)
            movie.deleteLater()

    def _evict(self) -> None:
        while self._cache and (self._bytes > self.max_bytes or self._movies > self.max_movies):
            _, entry = self._cache.popitem(last=False)
            if isinstance(entry, tuple):
                self._bytes -= max(64 * 1024, _estimate_bytes(entry[0].currentImage()))
                self._discard(entry)
            else:
                self._bytes -= max(64 * 1024, _estimate_bytes(entry.toImage()))


# Module-level singleton: widgets and services import this directly.
image_cache = ImageCache()
