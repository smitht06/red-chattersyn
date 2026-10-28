"""Application bootstrap: QApplication + qasync event loop, clean shutdown."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import shutil
import sys
import traceback
from pathlib import Path

import qasync
from PySide6.QtCore import QCoreApplication, QStandardPaths
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .config.paths import cache_dir, config_dir
from .config.settings import Settings
from .core.disk_cache import DiskCache
from .core.image_cache import image_cache
from .ui import theme
from .ui.main_window import MainWindow

log = logging.getLogger("chattersyn")

# v0.1.x used org "ChatterSync"; the rename moves the config dir, so carry
# the user's files over once. Keyring entries are unaffected (service name
# "red-chattersyn" did not change).
_LEGACY_ORG = "ChatterSync"

# Rendered from packaging/icon.svg by scripts/replace_logo.py (/replace-logo).
ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon.png"


def _apply_window_icon(app: QApplication) -> None:
    # A frozen macOS .app already shows its bundle icon (icon.icns, every size
    # up to 1024 px) in the Dock, and setting one here would replace it.
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        return
    app.setWindowIcon(QIcon(str(ICON_PATH)))


def _migrate_legacy_config(old_dir: Path, new_dir: Path) -> None:
    if not old_dir.is_dir() or new_dir.exists():
        return
    new_dir.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "secrets.json"):
        src = old_dir / name
        if src.is_file():
            shutil.copy2(src, new_dir / name)
            log.info("migrated %s from %s", name, old_dir)


def _install_excepthook() -> None:
    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("Unhandled exception:\n%s", text)
        try:
            crash = config_dir() / "crash.log"
            crash.parent.mkdir(parents=True, exist_ok=True)
            with crash.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError:
            pass
        with contextlib.suppress(Exception):  # pragma: no cover - dialog unavailable
            QMessageBox.critical(None, "ChatterSyn",
                                 f"Unhandled error:\n{exc}\n\nDetails written to crash.log")

    sys.excepthook = hook


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exc = context.get("exception")
    message = context.get("message", "asyncio task error")
    log.error("Event loop error: %s (%r)", message, exc)


def _prune_disk_cache(disk: DiskCache) -> None:
    try:
        disk.prune()
    except OSError as exc:
        logging.getLogger(__name__).debug("image disk cache prune failed: %s", exc)


async def _main(app: QApplication, settings: Settings, demo: bool) -> None:
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    image_cache.setParent(app)  # keep it owned by the application
    # Downloaded emote/badge/avatar bytes persist between runs; trim the
    # cache off the GUI thread once per start.
    image_cache.disk = DiskCache(cache_dir() / "images")
    loop.run_in_executor(None, _prune_disk_cache, image_cache.disk)

    window = MainWindow(settings, demo=demo)
    window.show()

    # The default quitOnLastWindowClosed stops the Qt event loop the moment
    # the last window closes, before the async shutdown below can run — qasync's
    # run_until_complete then aborts with "Event loop stopped before Future
    # completed". Take over quitting instead: wait on lastWindowClosed (which
    # fires WITHOUT stopping the loop), stop the services while it still runs,
    # then return — the future's completion callback stops the loop for real.
    app.setQuitOnLastWindowClosed(False)
    quit_event = asyncio.Event()
    app.lastWindowClosed.connect(quit_event.set)
    await quit_event.wait()

    # Stop network services before the loop closes (they need a running loop).
    await window.shutdown()

    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chattersyn", description="ChatterSyn dual chat client")
    parser.add_argument("--demo", action="store_true",
                        help="start with the built-in fake feed (no credentials needed)")
    args = parser.parse_args(argv)

    QCoreApplication.setOrganizationName("ChatterSyn")
    QCoreApplication.setApplicationName("chattersyn")

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app = QApplication.instance() or QApplication(sys.argv[:1] if argv else sys.argv)
    _apply_window_icon(app)
    new_dir = Path(QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation))
    QCoreApplication.setOrganizationName(_LEGACY_ORG)  # resolve the old dir…
    _migrate_legacy_config(
        Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)),
        new_dir,
    )
    QCoreApplication.setOrganizationName("ChatterSyn")  # …then switch back
    settings = Settings.load(config_dir())
    theme.apply(app, settings.get("theme", "dark"), settings.get("ui_scale", 1.0))
    _install_excepthook()

    asyncio.run(_main(app, settings, demo=args.demo), loop_factory=qasync.QEventLoop)
    return 0
