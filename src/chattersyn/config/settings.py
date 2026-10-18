"""settings.json load/save with schema defaults (Chatterino-style JSON config)."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "theme": "dark",
    "ui_scale": 1.0,
    "scrollback": 1000,
    "timestamps": {"enabled": True, "format": "HH:mm:ss"},
    "tabs": [],
    "general": {"restore_tabs": True},
    "send": {"truncate_mode": "highlight", "block_urls_for_youtube": True},
    "twitch": {"client_id": "", "redirect_ports": [17821, 17822, 17823]},
    "hotkeys": {
        "open_settings": "Ctrl+,",
        "new_tab": "Ctrl+T",
        "close_tab": "Ctrl+W",
        "next_tab": "Ctrl+Tab",
        "prev_tab": "Ctrl+Shift+Tab",
        "focus_input": "Ctrl+L",
        "toggle_search": "Ctrl+F",
        "toggle_theme": "",
        "emote_picker": "Ctrl+E",
    },
    # The Data API key lives in the OS keyring (SecretsStore), not here. A
    # legacy plain-text key in settings.json is migrated into the keyring on
    # first service construction. Twitch has no secret at all (PKCE public
    # client); twitch.client_id above is only an override for the baked-in ID.
    "youtube": {},
    "emote_providers": {
        "twitch": True,
        "7tv": True,
        "bttv": True,
        "ffz": True,
        "emote_priority": ["twitch", "7tv", "bttv", "ffz"],
    },
    "highlights": [],
    "highlight_color": "#7a5cff",
    "favourites": {"emotes": [], "emojis": []},  # two names-only lists (legacy [] = emotes)
    "autocomplete": {"emotes": True, "users": True, "commands": True},
    "commands": [],
    "moderation_actions": [],
    "notifications": {"mentions": True, "sound": False, "live": False},
    "logging": {"enabled": True, "format": "[HH:mm:ss] {name}: {msg}", "path": ""},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Override nested dicts without dropping unknown keys."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Settings(QObject):
    """Application settings; emits `changed` whenever a value is set or saved."""

    changed = Signal()

    def __init__(self, data: dict[str, Any] | None = None, path: Path | None = None):
        super().__init__()
        self._data = _deep_merge(DEFAULTS, data or {})
        self._path = path

    @classmethod
    def load(cls, config_dir: Path) -> Settings:
        path = config_dir / "settings.json"
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                log.warning("Could not read %s: %s — using defaults", path, exc)
        return cls(data, path)

    @property
    def path(self) -> Path | None:
        return self._path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError(f"{dotted!r}: {part} is not a mapping")
        node[parts[-1]] = copy.deepcopy(value)
        self.changed.emit()

    def set_batch(self, values: dict[str, Any]) -> None:
        """Set several keys and save once (mutations that must persist)."""
        for dotted, value in values.items():
            self.set(dotted, value)
        self.save()

    def save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)
