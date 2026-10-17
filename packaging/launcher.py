"""Frozen-app entry point (PyInstaller target). Mirrors scripts/run.py
without the dev-only venv self-heal; the app entry is chattersyn.app:run."""
import sys

from chattersyn.app import run

if __name__ == "__main__":
    sys.exit(run())
