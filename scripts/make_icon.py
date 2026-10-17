"""Draw the ChatterSync app icon: rounded tile + dual chat bubble, brand
purple on the app's dark palette. Writes packaging/icon.png and the derived
packaging/icon.icns + packaging/icon.ico (build inputs for PyInstaller).

Run headless with QT_QPA_PLATFORM=offscreen. Deterministic — no timestamps.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath
from PySide6.QtGui import QPen

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging"
SIZE = 1024
SIZES = [16, 32, 64, 128, 256, 512, 1024]

TILE_RADIUS = 180.0
BUBBLE_RADIUS = 110.0
PURPLE_TOP = QColor("#8f6bff")
PURPLE_BOTTOM = QColor("#5a3fd9")
TILE_TOP = QColor("#2e2744")
TILE_BOTTOM = QColor("#1c1830")


def _tile_path() -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, SIZE, SIZE), TILE_RADIUS, TILE_RADIUS)
    return path


def _bubble_path() -> QPainterPath:
    bubble = QRectF(212, 212, 600, 420)
    path = QPainterPath()
    path.addRoundedRect(bubble, BUBBLE_RADIUS, BUBBLE_RADIUS)
    # Tail bottom-left, like a speech bubble pointing at the viewer.
    tail = QPainterPath()
    tail.moveTo(310, 600)
    tail.lineTo(230, 760)
    tail.lineTo(430, 632)
    tail.closeSubpath()
    return path.united(tail)


def draw() -> QImage:
    image = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    tile_gradient = QLinearGradient(QPointF(0, 0), QPointF(0, SIZE))
    tile_gradient.setColorAt(0.0, TILE_TOP)
    tile_gradient.setColorAt(1.0, TILE_BOTTOM)
    painter.setBrush(tile_gradient)
    painter.setPen(QPen(QColor(0, 0, 0, 60), 4.0))
    painter.drawPath(_tile_path())

    bubble_gradient = QLinearGradient(QPointF(0, 212), QPointF(0, 632))
    bubble_gradient.setColorAt(0.0, PURPLE_TOP)
    bubble_gradient.setColorAt(1.0, PURPLE_BOTTOM)
    painter.setBrush(bubble_gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPath(_bubble_path())
    # Three chat dots.
    painter.setBrush(QColor("#ffffff"))
    for cx in (342, 512, 682):
        painter.drawEllipse(QPointF(cx, 422), 54, 54)

    painter.end()
    return image


def main() -> int:
    OUT.mkdir(exist_ok=True)
    # QGuiApplication for gradient infrastructure; never shows a window.
    QGuiApplication.instance() or QGuiApplication(sys.argv[:1])

    png = OUT / "icon.png"
    draw().save(str(png))
    print(f"wrote {png}")

    from PIL import Image  # noqa: PLC0415 (build-only dep)

    with Image.open(png) as base:
        sized = [base.resize((s, s), Image.Resampling.LANCZOS) for s in SIZES]
        base.save(OUT / "icon.icns", format="ICNS", append_images=sized)
        base.save(OUT / "icon.ico", format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {OUT / 'icon.icns'} and {OUT / 'icon.ico'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
