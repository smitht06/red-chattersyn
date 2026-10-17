"""Configuration directory resolution (needs a QCoreApplication to exist)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths


def config_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    return Path(base) if base else Path.home() / ".config" / "chattersyn"
