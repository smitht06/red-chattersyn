"""Application bootstrap: QApplication + qasync event loop, clean shutdown."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import traceback

import qasync
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from .config.paths import config_dir
from .config.settings import Settings
from .core.image_cache import image_cache
from .ui import theme
from .ui.main_window import MainWindow

log = logging.getLogger("chattersyn")


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
            QMessageBox.critical(None, "ChatterSync",
                                 f"Unhandled error:\n{exc}\n\nDetails written to crash.log")

    sys.excepthook = hook


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exc = context.get("exception")
    message = context.get("message", "asyncio task error")
    log.error("Event loop error: %s (%r)", message, exc)


async def _main(app: QApplication, settings: Settings, demo: bool) -> None:
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    image_cache.setParent(app)  # keep it owned by the application

    window = MainWindow(settings, demo=demo)
    window.show()

    # Clean shutdown: aboutToQuit just sets the event and cancels tasks; the
    # main coroutine then drains them BEFORE asyncio.run closes the loop.
    quit_event = asyncio.Event()
    app.aboutToQuit.connect(quit_event.set)
    await quit_event.wait()

    # Stop network services before the loop closes (they need a running loop).
    await window.shutdown()

    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chattersyn", description="ChatterSync dual chat client")
    parser.add_argument("--demo", action="store_true",
                        help="start with the built-in fake feed (no credentials needed)")
    args = parser.parse_args(argv)

    QCoreApplication.setOrganizationName("ChatterSync")
    QCoreApplication.setApplicationName("chattersyn")

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app = QApplication.instance() or QApplication(sys.argv[:1] if argv else sys.argv)
    settings = Settings.load(config_dir())
    theme.apply(app, settings.get("theme", "dark"), settings.get("ui_scale", 1.0))
    _install_excepthook()

    asyncio.run(_main(app, settings, demo=args.demo), loop_factory=qasync.QEventLoop)
    return 0
