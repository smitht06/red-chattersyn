#!/usr/bin/env python3
"""Replace the ChatterSyn logo from an SVG, for the /replace-logo skill.

Usage::

    replace_logo.py check LOGO.svg [--preview DIR]   # validate; changes nothing in the repo
    replace_logo.py apply LOGO.svg [--preview DIR]   # validate, then make it the logo

The SVG is the master. ``apply`` copies it to packaging/icon.svg and renders
these rasters straight from the vectors:

    packaging/icon.png              1024x1024 build input: make_icon.py derives
                                    the macOS .icns and Windows .ico from it,
                                    build_deb.py the Linux hicolor icons.
    src/chattersyn/assets/icon.png  256x256 window/taskbar icon (app.py).

Each PNG records the SVG's SHA-256 (PNG text key ``Source-SHA256``), so
tests/test_replace_logo.py fails when icon.svg changes without a re-render.

Only SVG is accepted, because bitmaps lose quality when scaled up. ``check``
rejects what wouldn't render the same everywhere (text, which depends on the
installed fonts; external files; scripts) and warns about embedded bitmaps
that get upscaled, non-square art and an opaque background. ``--preview``
writes the 1024 render plus a sheet of the small sizes on dark and light
backgrounds. Exit status: 0 ok (warnings allowed), 1 errors.

Renders with QtSvg, the app's own SVG engine, onto QImages without a
QGuiApplication, so no Qt platform plugin is needed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv_fix import unhide_venv_files  # noqa: E402

unhide_venv_files()

from PySide6.QtCore import QByteArray, QRectF, Qt, qInstallMessageHandler
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
MASTER_SVG = ROOT / "packaging" / "icon.svg"
# Every raster rendered from the master: (path, size in px).
OUTPUTS = (
    (ROOT / "packaging" / "icon.png", 1024),
    (ROOT / "src" / "chattersyn" / "assets" / "icon.png", 256),
)
SOURCE_KEY = "Source-SHA256"
PREVIEW_SIZES = (16, 32, 64, 128, 256)
PREVIEW_BACKGROUNDS = ("#1e1e1e", "#f2f2f2")  # dark Dock/taskbar, light Finder/menus

# Elements that don't render the same on every machine, with the fix.
_FORBIDDEN = {
    "text": "text depends on the installed fonts; convert it to outlines (paths)",
    "foreignObject": "embedded HTML isn't rendered; redraw it as shapes",
    "script": "scripts aren't allowed in the logo",
}
_HREF_KEYS = ("href", "{http://www.w3.org/1999/xlink}href")
_LARGEST = max(size for _, size in OUTPUTS)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def show(self) -> None:
        for kind, lines in (("error", self.errors), ("warning", self.warnings), ("note", self.notes)):
            for line in lines:
                print(f"{kind}: {line}")


class _QtMessages:
    """Collects Qt's warnings (QtSvg reports what it can't draw that way)."""

    def __enter__(self) -> list[str]:
        self.lines: list[str] = []
        self._previous = qInstallMessageHandler(lambda _mode, _ctx, msg: self.lines.append(msg))
        return self.lines

    def __exit__(self, *_exc) -> None:
        qInstallMessageHandler(self._previous)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _length(value: str | None, default: float) -> float:
    try:
        return float((value or "").removesuffix("px"))
    except ValueError:  # missing, "100%", other units
        return default


def render(data: bytes, size: int) -> QImage:
    """The SVG drawn at size x size px on transparency, centered if not square."""
    renderer = QSvgRenderer(QByteArray(data))
    box = renderer.viewBoxF()
    scale = size / max(box.width(), box.height())
    width, height = box.width() * scale, box.height() * scale
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(painter, QRectF((size - width) / 2, (size - height) / 2, width, height))
    painter.end()
    return image


def _solid_color(image: QImage) -> str | None:
    """The image's only color as #rrggbb (#aarrggbb if translucent), else None."""
    argb = image.convertToFormat(QImage.Format.Format_ARGB32)  # referenced: constBits() is a view
    pixels = bytes(argb.constBits())  # 4 bytes per pixel, no row padding
    if pixels != pixels[:4] * (len(pixels) // 4):
        return None
    color = QColor.fromRgba(argb.pixel(0, 0))
    return color.name(QColor.NameFormat.HexArgb) if color.alpha() < 255 else color.name()


def _check_bitmap(element: ET.Element, href: str, box: QRectF, report: Report) -> None:
    name = element.get("id") or "unnamed"
    header, _, payload = href.partition(",")
    if not header.endswith(";base64"):
        report.warnings.append(f"bitmap '{name}' isn't base64-encoded; check the preview")
        return
    try:
        image = QImage.fromData(base64.b64decode("".join(payload.split())))
    except ValueError:  # binascii.Error: corrupt base64
        image = QImage()
    if image.isNull():
        report.warnings.append(f"bitmap '{name}' couldn't be decoded; check the preview")
        return
    color = _solid_color(image)
    if color is not None:
        report.notes.append(f"bitmap '{name}' is a solid {color} fill (scales without loss)")
        return
    drawn = _length(element.get("width"), box.width()) * _LARGEST / max(box.width(), box.height())
    if drawn > image.width() * 1.05:
        report.warnings.append(
            f"bitmap '{name}' is only {image.width()}x{image.height()} px but is drawn about "
            f"{drawn:.0f} px wide in the {_LARGEST} px icon, so it gets blurry; replace it with "
            "vector shapes")


def check(path: Path) -> tuple[Report, bytes | None]:
    """Validate a candidate logo. Returns the report and the SVG bytes (None
    when the file can't be used at all)."""
    report = Report()
    if path.suffix.lower() != ".svg":
        report.errors.append(
            f"{path.name} is not an .svg file. Export the logo as SVG (vector): bitmaps "
            f"(PNG, JPEG, ICNS, ...) lose quality when scaled up to {_LARGEST} px")
        return report, None
    data = path.read_bytes()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        report.errors.append(f"not valid SVG (XML error: {exc})")
        return report, None
    if _local(root.tag) != "svg":
        report.errors.append(f"the root element is <{_local(root.tag)}>, not <svg>")
        return report, None

    names = {_local(element.tag) for element in root.iter()}
    for name in sorted(names & _FORBIDDEN.keys()):
        report.errors.append(f"contains <{name}>: {_FORBIDDEN[name]}")
    bitmaps = []
    for element in root.iter():
        href = next((element.get(key) for key in _HREF_KEYS if element.get(key)), None)
        if href is None or href.startswith("#"):
            continue
        if not href.startswith("data:"):
            report.errors.append(
                f"<{_local(element.tag)}> links to an outside file ({href[:60]}); embed it or "
                "redraw it as shapes")
        elif _local(element.tag) == "image":
            bitmaps.append((element, href))
    if report.errors:
        # Stop before rendering: drawing <text> needs a QGuiApplication's fonts.
        return report, data

    with _QtMessages() as qt_messages:
        renderer = QSvgRenderer(QByteArray(data))
        box = renderer.viewBoxF()
        if not renderer.isValid() or box.width() <= 0 or box.height() <= 0:
            report.errors.append("Qt's SVG renderer can't read it, or it has no size (set a viewBox)")
            return report, None
        sample = render(data, 128)

    square = abs(box.width() - box.height()) <= 0.01 * max(box.width(), box.height())
    report.notes.insert(0, f"canvas {box.width():g} x {box.height():g}" + (", square" if square else ""))
    if not square:
        report.warnings.append("not square; it's centered on a transparent square canvas")
    for element, href in bitmaps:
        _check_bitmap(element, href, box, report)

    # Keep the converted image referenced: constBits() is a view into its
    # buffer, which a temporary QImage would free before bytes() copies it.
    alpha_image = sample.convertToFormat(QImage.Format.Format_Alpha8)
    if not any(bytes(alpha_image.constBits())):
        report.errors.append("it renders as an empty, fully transparent image")
    elif min(sample.pixelColor(x, y).alpha() for x in (0, 127) for y in (0, 127)) == 255:
        report.warnings.append(
            "opaque background: the corners are solid, so the Dock, taskbar and launchers show "
            "a full square tile instead of the logo's own shape")
    for line in dict.fromkeys(qt_messages):
        report.warnings.append(f"Qt's SVG renderer: {line}")
    return report, data


def write_preview(data: bytes, out_dir: Path) -> list[Path]:
    """The full-size render, plus PREVIEW_SIZES side by side on each background."""
    out_dir.mkdir(parents=True, exist_ok=True)
    full = out_dir / "logo-1024.png"
    render(data, _LARGEST).save(str(full))

    gap = 24
    row = max(PREVIEW_SIZES) + 2 * gap
    width = sum(PREVIEW_SIZES) + gap * (len(PREVIEW_SIZES) + 1)
    sheet = QImage(width, row * len(PREVIEW_BACKGROUNDS), QImage.Format.Format_ARGB32_Premultiplied)
    renders = {size: render(data, size) for size in PREVIEW_SIZES}
    painter = QPainter(sheet)
    for index, background in enumerate(PREVIEW_BACKGROUNDS):
        top = index * row
        painter.fillRect(0, top, width, row, QColor(background))
        x = gap
        for size in PREVIEW_SIZES:
            painter.drawImage(x, top + row - gap - size, renders[size])  # bottom-aligned
            x += size + gap
    painter.end()
    sizes = out_dir / "logo-sizes.png"
    sheet.save(str(sizes))
    return [full, sizes]


def apply(src: Path, data: bytes) -> list[Path]:
    """Make ``data`` (read from ``src``) the logo. Returns the files written."""
    written = []
    if src.resolve() != MASTER_SVG.resolve():
        MASTER_SVG.write_bytes(data)
        written.append(MASTER_SVG)
    digest = hashlib.sha256(data).hexdigest()
    for path, size in OUTPUTS:
        image = render(data, size)
        image.setText(SOURCE_KEY, digest)
        if not image.save(str(path), "PNG"):
            raise OSError(f"couldn't write {path}")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("check", "validate only; changes nothing in the repo"),
                            ("apply", "validate, then make it the app logo")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("svg", type=Path)
        cmd.add_argument("--preview", type=Path, metavar="DIR",
                         help="also write logo-1024.png and logo-sizes.png here")
    args = parser.parse_args(argv)

    if not args.svg.is_file():
        print(f"error: {args.svg} not found")
        return 1
    report, data = check(args.svg)
    report.show()
    if data is None or report.errors:
        return 1
    if args.preview:
        for path in write_preview(data, args.preview):
            print(f"preview: {path}")
    if args.cmd == "apply":
        for path in apply(args.svg, data):
            print(f"wrote {path}")
        print("next: uv run --group build python scripts/make_icon.py  (the .icns and .ico)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
