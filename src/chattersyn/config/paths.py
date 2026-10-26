"""Configuration directory resolution (needs a QCoreApplication to exist)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths


def config_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    return Path(base) if base else Path.home() / ".config" / "chattersyn"


def cache_dir() -> Path:
    """Disposable data (downloaded images); safe to delete at any time."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    return Path(base) if base else Path.home() / ".cache" / "chattersyn"
