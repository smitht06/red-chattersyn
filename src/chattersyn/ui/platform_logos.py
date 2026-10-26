"""Platform logos (Twitch glitch, YouTube play button) marking each chat row's
source. Inline SVG rendered through QtSvg — no image assets to bundle — and
cached per (platform, size, device pixel ratio) so HiDPI rows stay crisp."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ..models.message import Platform

# 24x24 viewBox outlines (Simple Icons), in the brands' own colours: the
# Twitch glitch gets a white face behind the purple so the eyes read on dark
# rows; the YouTube button gets its white play triangle.
_SVG = {
    Platform.TWITCH: (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path fill="#ffffff" d="M20.571 11.143l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714'
        'h13.714Z"/>'
        '<path fill="#9146ff" d="M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143h-1.714z'
        'M6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143'
        'l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714h13.714Z"/></svg>'
    ),
    Platform.YOUTUBE: (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path fill="#ff0000" d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545'
        ' 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12'
        ' 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376'
        '.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93'
        '-.502-5.814z"/>'
        '<path fill="#ffffff" d="M9.545 15.568V8.432L15.818 12l-6.273 3.568z"/></svg>'
    ),
}

_cache: dict[tuple[Platform, int, float], QPixmap] = {}


def platform_logo(platform: Platform, size: int, dpr: float = 1.0) -> QPixmap | None:
    """`size`x`size` logical-pixel logo for `platform`, or None if it has none."""
    svg = _SVG.get(platform)
    if svg is None:
        return None
    key = (platform, size, round(dpr, 2))
    pixmap = _cache.get(key)
    if pixmap is None:
        side = max(1, round(size * dpr))
        pixmap = QPixmap(side, side)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        QSvgRenderer(QByteArray(svg.encode("ascii"))).render(painter, QRectF(0, 0, side, side))
        painter.end()
        pixmap.setDevicePixelRatio(dpr)
        _cache[key] = pixmap
    return pixmap
