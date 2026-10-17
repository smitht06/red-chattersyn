"""OAuth token storage: keyring (macOS Keychain / Windows Credential Manager /
Linux Secret Service) with a 0600-permission JSON file fallback."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SERVICE = "red-chattersyn"


class SecretsStore:
    def __init__(self, config_dir: Path):
        self._config_dir = config_dir
        self._fallback: Path | None = None
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
        tmp.write_text(json.dumps(data), encoding="utf-8")
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
