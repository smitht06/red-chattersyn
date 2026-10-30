"""Diagnostic log: a rotating file that every build writes, credentials redacted.

Packaged GUI builds have no usable stderr (in the windowed Windows build
sys.stderr is None), so without a file nothing survives for a bug report.
Chat logs are a separate feature (services/logger.py, config_dir()/logs).
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

LOG_NAME = "chattersyn.log"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 1_000_000
BACKUPS = 4

_REDACTIONS = (
    # Twitch chat tokens (IRC PASS) and Authorization header values.
    (re.compile(r"oauth:[A-Za-z0-9]+"), "oauth:<redacted>"),
    (re.compile(r"(?i)\b(bearer|oauth)(\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1\2<redacted>"),
    # Google API keys and access tokens.
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "<redacted-api-key>"),
    (re.compile(r"ya29\.[0-9A-Za-z._-]+"), "ya29.<redacted>"),
    # Token fields in JSON, dict reprs and query strings.
    (re.compile(r"(?i)\b(access_token|refresh_token|id_token|client_secret|code_verifier)"
                r"(['\"]?\s*[:=]\s*['\"]?)[^'\"&\s,}]+"), r"\1\2<redacted>"),
    # OAuth redirect authorization codes (?code=...), not "status code: 404".
    (re.compile(r"([?&]code=)[^&\s'\"]+"), r"\1<redacted>"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(directory: Path, level: int = logging.INFO) -> Path | None:
    """Send the root logger to stderr (when there is one) and to a rotating
    file in ``directory``. Safe to call again: it replaces its own handlers.
    Returns the log file, or None when the file can't be opened."""
    root = logging.getLogger()
    root.setLevel(level)
    # httpx logs every request at INFO, one line per emote image; failures
    # still come through at WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for handler in [h for h in root.handlers if getattr(h, "_chattersyn", False)]:
        root.removeHandler(handler)
        handler.close()

    formatter = RedactingFormatter(FORMAT)
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    path: Path | None = directory / LOG_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"))
    except OSError:
        path = None
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._chattersyn = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    if path is None:
        logging.getLogger(__name__).warning("diagnostic log unavailable in %s", directory)
    return path
