"""ImageCache: animated requests only become QMovies for multi-frame data."""

import asyncio
import base64

import httpx
import respx
from PySide6.QtGui import QMovie, QPixmap

from chattersyn.core.image_cache import ImageCache

# 1x1 single-frame GIF (also used by test_delegate).
ONE_FRAME_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")
_HEADER = ONE_FRAME_GIF[:19]  # GIF89a + screen descriptor + 2-colour table
_FRAME = (b"\x21\xf9\x04\x00\x0a\x00\x00\x00"  # graphic control: 100 ms delay
          + ONE_FRAME_GIF[19:-1])              # image descriptor + pixel data
TWO_FRAME_GIF = _HEADER + _FRAME + _FRAME + b"\x3b"


async def _fetch(cache: ImageCache, url: str, animated: bool):
    await cache._fetch(url, animated, cache._key(url, animated))
    return cache.request(url, animated)


@respx.mock
async def test_animated_request_plays_multi_frame_and_keeps_static_as_pixmap(qapp):
    respx.get("https://cdn.example/anim.gif").mock(
        return_value=httpx.Response(200, content=TWO_FRAME_GIF))
    respx.get("https://cdn.example/still.gif").mock(
        return_value=httpx.Response(200, content=ONE_FRAME_GIF))
    cache = ImageCache()
    try:
        anim = await _fetch(cache, "https://cdn.example/anim.gif", True)
        still = await _fetch(cache, "https://cdn.example/still.gif", True)
    finally:
        await cache._client.aclose()
    assert isinstance(anim, QMovie) and anim.frameCount() == 2
    # A static image asked for as "animated" (e.g. any Twitch emote) must not
    # use up a movie slot.
    assert isinstance(still, QPixmap) and not still.isNull()
    assert cache._movies == 1
    cache.clear()


@respx.mock
async def test_disk_cache_hit_skips_network_and_miss_writes(qapp, tmp_path):
    from chattersyn.core.disk_cache import DiskCache

    route = respx.get("https://cdn.example/still.gif").mock(
        return_value=httpx.Response(200, content=ONE_FRAME_GIF))
    disk = DiskCache(tmp_path)
    first = ImageCache()
    first.disk = disk
    try:
        assert isinstance(await _fetch(first, "https://cdn.example/still.gif", False), QPixmap)
    finally:
        await first._client.aclose()
    assert route.call_count == 1
    assert disk.get("https://cdn.example/still.gif") == ONE_FRAME_GIF  # miss -> written

    second = ImageCache()  # a fresh run: memory cache empty, disk warm
    second.disk = disk
    pixmap = await _fetch(second, "https://cdn.example/still.gif", False)
    assert isinstance(pixmap, QPixmap) and not pixmap.isNull()
    assert route.call_count == 1  # served from disk, no request
    assert second._client is None


@respx.mock
async def test_undecodable_bytes_never_hit_disk(qapp, tmp_path):
    from chattersyn.core.disk_cache import DiskCache

    respx.get("https://cdn.example/junk").mock(return_value=httpx.Response(200, content=b"nope"))
    cache = ImageCache()
    cache.disk = DiskCache(tmp_path)
    try:
        assert await _fetch(cache, "https://cdn.example/junk", False) is None
    finally:
        await cache._client.aclose()
    assert cache.disk.get("https://cdn.example/junk") is None


# -- size cap (LD-7, LD-8) -----------------------------------------------------------------


async def _fetch_recording(cache, url, monkeypatch):
    """Fetch `url`; returns (decoded byte payloads, image_ready urls)."""
    from chattersyn.core import image_cache as mod
    decoded, ready = [], []
    real = mod._decode_image
    monkeypatch.setattr(mod, "_decode_image", lambda data: decoded.append(len(data)) or real(data))
    cache.image_ready.connect(ready.append)
    try:
        await cache._fetch(url, False, cache._key(url, False))
    finally:
        if cache._client is not None:
            await cache._client.aclose()
    return decoded, ready


@respx.mock
async def test_oversized_image_skipped(qapp, caplog, monkeypatch):
    """LD-7: a 9 MB body is never decoded or shown, and the log names the URL."""
    url = "https://cdn.test/huge.png"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"\0" * (9 * 1024 * 1024)))
    caplog.set_level("DEBUG", logger="chattersyn.core.image_cache")
    decoded, ready = await _fetch_recording(ImageCache(), url, monkeypatch)
    assert decoded == [] and ready == []
    assert url in caplog.text and "too large" in caplog.text


@respx.mock
async def test_normal_image_loads(qapp, monkeypatch):
    """LD-8."""
    url = "https://cdn.test/emote.gif"
    respx.get(url).mock(return_value=httpx.Response(200, content=ONE_FRAME_GIF))
    decoded, ready = await _fetch_recording(ImageCache(), url, monkeypatch)
    assert decoded == [len(ONE_FRAME_GIF)]
    assert ready == [url]


@respx.mock
async def test_cap_without_content_length(qapp, caplog, monkeypatch):
    """Review Focus 3: a streamed body without Content-Length is cut off too."""
    url = "https://cdn.test/chunked.webp"
    chunk = b"\0" * (1024 * 1024)

    async def body():
        for _ in range(9):
            yield chunk
    respx.get(url).mock(return_value=httpx.Response(200, content=body()))
    caplog.set_level("DEBUG", logger="chattersyn.core.image_cache")
    decoded, ready = await _fetch_recording(ImageCache(), url, monkeypatch)
    assert decoded == [] and ready == []
    assert "too large" in caplog.text


@respx.mock
async def test_refused_image_is_not_downloaded_again(qapp, monkeypatch):
    """Final review #1: a refused URL isn't re-fetched on every repaint."""
    url = "https://cdn.test/huge-again.png"
    route = respx.get(url).mock(
        return_value=httpx.Response(200, content=b"\0" * (9 * 1024 * 1024)))
    cache = ImageCache()
    try:
        await cache._fetch(url, False, cache._key(url, False))
        for _ in range(3):  # repaints ask again
            assert cache.request(url) is None
        await asyncio.sleep(0.05)
    finally:
        if cache._client is not None:
            await cache._client.aclose()
    assert route.call_count == 1
