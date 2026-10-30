"""scripts/replace_logo.py (the /replace-logo skill) and the committed logo renders."""

import base64
import hashlib

import pytest
import replace_logo
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from replace_logo import SOURCE_KEY, check, main, render

CIRCLE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
          '<circle cx="50" cy="50" r="40" fill="#e33"/></svg>')


def _write(tmp_path, body: str, name: str = "logo.svg"):
    path = tmp_path / name
    path.write_text(body)
    return path


def _bitmap_svg(image: QImage) -> str:
    """A 100x100 SVG whose whole canvas is ``image``, embedded as a PNG."""
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    href = "data:image/png;base64," + base64.b64encode(bytes(buf.data())).decode()
    return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'viewBox="0 0 100 100"><image id="bg" width="100" height="100" xlink:href="{href}"/></svg>')


@pytest.fixture
def scratch_outputs(tmp_path, monkeypatch):
    """Point the script's master and renders at a scratch tree."""
    master = tmp_path / "packaging" / "icon.svg"
    outputs = ((tmp_path / "packaging" / "icon.png", 1024), (tmp_path / "assets" / "icon.png", 256))
    for path in (master, *(path for path, _ in outputs)):
        path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(replace_logo, "MASTER_SVG", master)
    monkeypatch.setattr(replace_logo, "OUTPUTS", outputs)


def test_committed_renders_match_the_master_svg():
    # Fails when packaging/icon.svg changed without a re-render. Fix: run
    # `uv run python scripts/replace_logo.py apply packaging/icon.svg`.
    digest = hashlib.sha256(replace_logo.MASTER_SVG.read_bytes()).hexdigest()
    for path, size in replace_logo.OUTPUTS:
        image = QImage(str(path))
        assert (image.width(), image.height()) == (size, size), path
        assert image.text(SOURCE_KEY) == digest, f"{path} is stale"


def test_clean_vector_logo_passes(tmp_path):
    report, data = check(_write(tmp_path, CIRCLE))
    assert data == CIRCLE.encode()
    assert report.errors == [] and report.warnings == []
    assert report.notes == ["canvas 100 x 100, square"]


@pytest.mark.parametrize("name", ["logo.png", "logo.jpg", "logo.icns"])
def test_bitmap_files_are_refused(tmp_path, name):
    report, data = check(_write(tmp_path, "pixels", name))
    assert data is None
    assert "is not an .svg file" in report.errors[0]


def test_files_that_are_not_svg_are_refused(tmp_path):
    report, data = check(_write(tmp_path, "<html><body/></html>"))
    assert data is None and "<html>" in report.errors[0]
    report, data = check(_write(tmp_path, "{not xml"))
    assert data is None and report.errors[0].startswith("not valid SVG")


def test_svg_tag_after_256_bytes_is_refused(tmp_path):
    # A long leading comment hid <svg from MIME sniffing: appstream then
    # failed the Flatpak build with "Unrecognized image file format".
    late = '<?xml version="1.0" encoding="UTF-8"?>\n<!-- ' + "x" * 300 + " -->\n" + CIRCLE
    report, _ = check(_write(tmp_path, late))
    assert "first 256 bytes" in report.errors[0]


def test_text_outside_files_and_scripts_are_errors(tmp_path):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           'viewBox="0 0 100 100"><text x="10" y="50">Hi</text>'
           '<image width="10" height="10" xlink:href="logo.png"/><script>alert(1)</script></svg>')
    report, _ = check(_write(tmp_path, svg))
    errors = "\n".join(report.errors)
    assert "<text>" in errors and "outlines" in errors
    assert "<script>" in errors
    assert "outside file (logo.png)" in errors


def test_upscaled_bitmap_is_flagged(tmp_path):
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    image.setPixelColor(0, 0, QColor("#000000"))
    report, _ = check(_write(tmp_path, _bitmap_svg(image)))
    assert any("only 8x8 px" in warning and "blurry" in warning for warning in report.warnings)


def test_solid_bitmap_background_is_noted_and_opaque_corners_warned(tmp_path):
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    report, _ = check(_write(tmp_path, _bitmap_svg(image)))
    assert "bitmap 'bg' is a solid #ffffff fill (scales without loss)" in report.notes
    assert any(warning.startswith("opaque background") for warning in report.warnings)
    assert not any("blurry" in warning for warning in report.warnings)


def test_non_square_logo_is_centered_on_transparency(tmp_path):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">'
           '<rect width="200" height="100" fill="#e33"/></svg>')
    report, data = check(_write(tmp_path, svg))
    assert any(warning.startswith("not square") for warning in report.warnings)
    assert not any(warning.startswith("opaque background") for warning in report.warnings)
    image = render(data, 64)  # 64x32 art, rows 16-47
    assert image.pixelColor(32, 4).alpha() == 0
    assert image.pixelColor(32, 32).alpha() == 255
    assert image.pixelColor(32, 60).alpha() == 0


def test_empty_logo_is_an_error(tmp_path):
    report, _ = check(_write(tmp_path, '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>'))
    assert "it renders as an empty, fully transparent image" in report.errors


def test_apply_installs_the_svg_and_renders_every_size(tmp_path, scratch_outputs):
    src = _write(tmp_path, CIRCLE, "new.svg")
    assert main(["apply", str(src), "--preview", str(tmp_path / "preview")]) == 0

    assert replace_logo.MASTER_SVG.read_text() == CIRCLE
    digest = hashlib.sha256(CIRCLE.encode()).hexdigest()
    for path, size in replace_logo.OUTPUTS:
        image = QImage(str(path))
        assert (image.width(), image.height()) == (size, size)
        assert image.text(SOURCE_KEY) == digest
        assert image.pixelColor(size // 2, size // 2) == QColor("#ee3333")
        assert image.pixelColor(0, 0).alpha() == 0
    assert (tmp_path / "preview" / "logo-1024.png").is_file()
    assert (tmp_path / "preview" / "logo-sizes.png").is_file()


def test_apply_refuses_a_logo_with_errors(tmp_path, scratch_outputs):
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><text>x</text></svg>'
    assert main(["apply", str(_write(tmp_path, svg, "new.svg"))]) == 1
    assert not replace_logo.MASTER_SVG.exists()
    assert not any(path.exists() for path, _ in replace_logo.OUTPUTS)
