"""Dev-environment self-heal: clear macOS UF_HIDDEN on the venv's .pth files
and Qt plugin directories.

The sandboxed shell marks files it creates as hidden, and re-applies the flag
when those files are touched. Consequences:
- Python 3.13 skips hidden .pth files (breaks the editable install).
- Qt's QDir skips hidden plugin dylibs (breaks QFactoryLoader -> "Could not
  find the Qt platform plugin", and the imageformats plugin fails to load on
  first decode — QImageReader then reports canRead=False for webp etc.).

Because the flag can come back between process start and the *lazy* first
image decode, ImageCache heals again right before decoding — but only after a
dev launcher opted in (scripts/_venv_fix.py calls enable_lazy_heal()).
Installed and frozen builds never run it: under `uv tool install` on macOS it
would spawn a chflags subprocess for every image.
"""

import glob
import os
import shutil
import subprocess
import sys

_lazy_heal = False


def enable_lazy_heal() -> None:
    """Dev launchers opt in to healing again before each lazy image decode."""
    global _lazy_heal
    _lazy_heal = True


def heal_before_decode() -> None:
    if _lazy_heal:
        unhide_venv_files()


def unhide_venv_files() -> None:
    prefix = sys.prefix
    if not prefix or not os.path.isdir(prefix):
        return
    targets: list[str] = []
    for site in glob.glob(os.path.join(prefix, "lib", "python*", "site-packages")):
        targets += glob.glob(os.path.join(site, "*.pth"))
        plugins = os.path.join(site, "PySide6", "Qt", "plugins")
        if os.path.isdir(plugins):
            targets.append(plugins)
    if targets and shutil.which("chflags"):
        subprocess.run(["chflags", "-R", "nohidden", *targets], check=False)
