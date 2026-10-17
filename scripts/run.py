"""Sandbox-friendly dev launcher: runs the app from src/ without relying on
the editable install (the sandboxed shell marks .pth files UF_HIDDEN, which
Python 3.13 skips). Usage: uv run python scripts/run.py [--demo]"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _venv_fix import unhide_venv_files  # noqa: E402

unhide_venv_files()

from chattersyn.app import run  # noqa: E402

if __name__ == "__main__":
    run(sys.argv[1:])
