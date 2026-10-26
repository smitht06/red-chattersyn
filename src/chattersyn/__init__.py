"""ChatterSyn — Chatterino-style dual chat client for Twitch + YouTube live chat."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

__version__ = "0.7.0"


def version() -> str:
    """Running app version.

    Prefers the installed package metadata (pip/uv read [project].version), and
    falls back to the frozen ``__version__`` constant inside a PyInstaller
    bundle, where there is no dist-info. scripts/release.sh keeps
    ``__version__`` in sync with pyproject.toml so the frozen fallback stays
    correct across releases.
    """
    try:
        return _installed_version("chattersyn")
    except PackageNotFoundError:
        return __version__
