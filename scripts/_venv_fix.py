"""Thin wrapper so pre-Qt import order stays simple; the implementation lives
in the package at chattersyn.core.sandbox_fix."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chattersyn.core.sandbox_fix import unhide_venv_files  # noqa: E402, F401
