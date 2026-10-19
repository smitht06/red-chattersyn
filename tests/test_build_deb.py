"""Staging-tree tests for the Linux .deb builder (no dpkg/Pillow required)."""

from pathlib import Path

import pytest
from build_deb import build_staging, deb_path


@pytest.fixture
def dist(tmp_path) -> Path:
    bundle = tmp_path / "chattersyn"
    bundle.mkdir()
    (bundle / "chattersyn").write_text("#!/bin/sh\necho hi\n")
    return tmp_path


def test_staging_layout_and_control(dist):
    staging = dist / "staging"
    build_staging(dist, "0.1.0", "amd64", staging, icons=False)

    control = (staging / "DEBIAN" / "control").read_text()
    assert "Version: 0.1.0" in control
    assert "Architecture: amd64" in control
    assert "__" not in control  # no placeholders left
    assert "Depends: libc6 (>= 2.35)" in control
    assert "Package: chattersyn" in control

    assert (staging / "opt" / "chattersyn" / "chattersyn").is_file()
    link = staging / "usr" / "bin" / "chattersyn"
    assert link.is_symlink()
    assert str(link.readlink()) == "/opt/chattersyn/chattersyn"
    desktop = (staging / "usr" / "share" / "applications" / "chattersyn.desktop").read_text()
    assert "Exec=/opt/chattersyn/chattersyn" in desktop
    assert "Icon=chattersyn" in desktop
    assert not (staging / "usr" / "share" / "icons").exists()


def test_staging_rebuild_overwrites(dist):
    staging = dist / "staging"
    build_staging(dist, "0.1.0", "amd64", staging, icons=False)
    (staging / "stale.txt").write_text("old")
    build_staging(dist, "0.2.0", "amd64", staging, icons=False)
    assert not (staging / "stale.txt").exists()
    assert "Version: 0.2.0" in (staging / "DEBIAN" / "control").read_text()


def test_staging_requires_bundle(dist):
    with pytest.raises(FileNotFoundError):
        build_staging(dist / "missing", "0.1.0", "amd64", dist / "staging", icons=False)


def test_deb_path_naming(dist):
    assert deb_path(dist, "0.1.0") == dist / "chattersyn-0.1.0-linux-x86_64.deb"
