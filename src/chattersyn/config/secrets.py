"""OAuth token storage: keyring (macOS Keychain / Windows Credential Manager /
Linux Secret Service) with a 0600-permission JSON file fallback."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

SERVICE = "red-chattersyn"


class SecretsStore:
    def __init__(self, config_dir: Path, on_fallback: Callable[[Path], None] | None = None):
        """``on_fallback`` is called once, with the file's path, the first time
        a credential has to go into the plaintext file instead of the keyring."""
        self._config_dir = config_dir
        self._fallback: Path | None = None
        self._on_fallback = on_fallback
        self._fallback_reported = False
        try:
            import keyring  # noqa: F401

            self._keyring = keyring
        except Exception:  # pragma: no cover - keyring absent
            self._keyring = None
            log.info("keyring unavailable — tokens will be stored in a local file")

    # -- keyring ------------------------------------------------------------

    def get(self, key: str) -> str | None:
        if self._keyring is not None:
            try:
                return self._keyring.get_password(SERVICE, key)
            except Exception as exc:  # keyring errors -> fall through to file
                log.warning("keyring get failed for %s: %s", key, exc)
        return self._file_get(key)

    def set(self, key: str, value: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.set_password(SERVICE, key, value)
            except Exception as exc:
                log.warning("keyring set failed for %s: %s — using file fallback", key, exc)
            else:
                return
        self._file_set(key, value)
        self._report_fallback()

    def _report_fallback(self) -> None:
        if self._fallback_reported:
            return
        self._fallback_reported = True
        path = self._fallback_path()
        log.warning("no usable system keyring — credentials are stored unencrypted in %s", path)
        if self._on_fallback is not None:
            self._on_fallback(path)

    def delete(self, key: str) -> None:
        if self._keyring is not None:
            with contextlib.suppress(Exception):
                self._keyring.delete_password(SERVICE, key)
        self._file_delete(key)

    # -- JSON file fallback --------------------------------------------------

    def _fallback_path(self) -> Path:
        if self._fallback is None:
            self._fallback = self._config_dir / "secrets.json"
        return self._fallback

    def _file_read(self) -> dict[str, str]:
        path = self._fallback_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _file_write(self, data: dict[str, str]) -> None:
        path = self._fallback_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        # Owner-only from the first byte: writing first and chmod-ing after
        # left the tokens readable by other users for a moment.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if hasattr(os, "fchmod"):  # a leftover tmp keeps its old mode
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data))
        tmp.replace(path)
        os.chmod(path, 0o600)

    def _file_get(self, key: str) -> str | None:
        return self._file_read().get(key)

    def _file_set(self, key: str, value: str) -> None:
        data = self._file_read()
        data[key] = value
        self._file_write(data)

    def _file_delete(self, key: str) -> None:
        data = self._file_read()
        if key in data:
            del data[key]
            self._file_write(data)
