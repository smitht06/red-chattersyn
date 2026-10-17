# NOTE: no QT_QPA_PLATFORM override here. Tests run on the native cocoa
# platform; widgets under test are never shown, so no windows appear.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from _venv_fix import unhide_venv_files  # noqa: E402

unhide_venv_files()

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def qapp():
    """Session QApplication. Autouse: the cocoa platform plugin aborts
    intermittently if the first QApplication is created late in a process
    where asyncio/httpx/grpc threads already ran — initialize Qt first."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
