"""Headless smoke run: boots ChatterSyn offscreen with the demo feed,
renders the window to a PNG after a few seconds, then quits.

Usage: uv run python scripts/screenshot.py [output.png]
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv_fix import unhide_venv_files  # noqa: E402

unhide_venv_files()

import qasync
from PySide6.QtWidgets import QApplication

from chattersyn.config.settings import Settings
from chattersyn.ui import theme
from chattersyn.ui.main_window import MainWindow


async def main(out_path: Path) -> None:
    window = MainWindow(Settings(), demo=True)
    # grab() renders the widget without needing it shown, so no window flashes.
    await asyncio.sleep(4.0)  # let the demo feed accumulate
    pixmap = window.grab()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    assert pixmap.save(str(out_path)), f"failed to save {out_path}"
    print(f"saved {out_path}")
    # Stop feeds while the loop is still running so their cancellation settles
    # cleanly instead of surfacing "no running event loop" at Runner teardown.
    for feed in window._feeds:
        feed.stop()
    await asyncio.sleep(0)
    window.close()
    await asyncio.sleep(0)
    QApplication.instance().quit()


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/chattersyn_m1.png")
    app = QApplication(sys.argv)
    theme.apply(app, "dark", 1.0)
    asyncio.run(main(out), loop_factory=qasync.QEventLoop)
