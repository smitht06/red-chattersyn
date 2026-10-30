"""Thin wrapper so pre-Qt import order stays simple; the implementation lives
in the package at chattersyn.core.sandbox_fix. Importing it marks the process
as a dev launch, so ImageCache also heals before each lazy image decode."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chattersyn.core.sandbox_fix import enable_lazy_heal, unhide_venv_files  # noqa: E402, F401

enable_lazy_heal()
