"""M2 spike: does animated WebP from the 7TV CDN actually play in QMovie
under PySide6 6.12? Fetches a real animated 7TV emote (forsen's set), loads it
as a QMovie and polls frame advancement on the qasync loop. Also verifies the
static first-frame fallback decodes.

Usage: uv run python scripts/spike_webp.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv_fix import unhide_venv_files  # noqa: E402

unhide_venv_files()

import httpx
import qasync
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImageReader, QMovie
from PySide6.QtWidgets import QApplication

from chattersyn.services.emotes.seventv import SevenTvProvider  # noqa: E402


async def main() -> None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        provider = SevenTvProvider(client)
        emotes = await provider.fetch_for_channel("xqc", "71092938")
        animated = [e for e in emotes if e.animated]
        if not animated:
            print("FAIL: no animated emotes found in forsen's 7TV set")
            QApplication.instance().quit()
            return
        emote = animated[0]
        url = emote.urls.get("2x") or emote.url
        print(f"emote: {emote.name} ({emote.id}) animated={emote.animated}")
        print(f"url: {url}")
        data = (await client.get(url)).content
        print(f"bytes: {len(data)}")

    # The sandbox re-applies UF_HIDDEN to plugin dylibs it sees touched; the
    # imageformats plugin is loaded lazily at first decode, so heal again now.
    unhide_venv_files()

    # Static first-frame fallback (what a broken webp plugin would force us to use).
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    static = QImageReader(buf)
    print(f"static decode: format={static.format().data().decode()} "
          f"size={static.size().width()}x{static.size().height()} canRead={static.canRead()}")

    # Animated path.
    buf2 = QBuffer()
    buf2.setData(QByteArray(data))
    buf2.open(QIODevice.OpenModeFlag.ReadOnly)
    movie = QMovie(buf2)
    print(f"movie: valid={movie.isValid()} frameCount={movie.frameCount()} "
          f"format={movie.format().data().decode()}")
    if movie.frameCount() <= 1:
        print("FAIL: QMovie reports a single frame — must use static fallback for webp")
        QApplication.instance().quit()
        return

    frames_seen: list[int] = []

    def on_frame(frame):
        frames_seen.append(frame)

    movie.frameChanged.connect(on_frame)
    movie.setCacheMode(QMovie.CacheMode.CacheAll)
    movie.start()
    await asyncio.sleep(1.2)
    movie.stop()
    advanced = len(set(frames_seen)) > 1
    print(f"frames observed in 1.2s: {len(frames_seen)} distinct={len(set(frames_seen))}")
    print("PASS: animated webp plays in QMovie" if advanced else
          "FAIL: movie never advanced frames")
    QApplication.instance().quit()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    with qasync.QEventLoop() as loop:
        loop.run_until_complete(main())
