"""ImageCache: animated requests only become QMovies for multi-frame data."""

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
