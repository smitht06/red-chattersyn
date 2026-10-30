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


def diagnostics_dir() -> Path:
    """The app's own diagnostic log (core/applog.py). Chat logs are separate,
    under config_dir() / "logs"."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    return (Path(base) if base else Path.home() / ".local" / "share" / "chattersyn") / "diagnostics"
