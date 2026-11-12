"""settings.json load/save with schema defaults (Chatterino-style JSON config)."""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

RESTORED_MESSAGE = "Your settings file was damaged — restored the last good copy."
NO_BACKUP_MESSAGE = ("Your settings file was damaged and no backup could be read — "
                     "started with default settings.")

DEFAULTS: dict[str, Any] = {
    "theme": "dark",
    "ui_scale": 1.0,
    "scrollback": 1000,
    "timestamps": {"enabled": True, "format": "HH:mm:ss"},
    "badges": {"enabled": True},
    # YouTube chat is plain text: emote names are removed from the YouTube
    # copy of a send (emote-only messages go to Twitch only), or become emoji
    # when `emoji` is on (map None = built-in DEFAULT_EMOJI; an edited table
    # replaces it wholesale). ChatterSyn always renders emotes on YouTube rows.
    "youtube_emotes": {"emoji": False, "map": None},
    # The OBS browser source (Settings → Browser Sources): the Stream tab's chat
    # on http://localhost:<port>/chat. font_family "" = the page's default font.
    "browser_source": {
        "enabled": False, "port": 17830, "font_family": "", "font_size": 20,
        "text_color": "#ffffff", "background": "#00000000",
        "background_fills_source": True, "outline": True,
        "show_platform_icons": True, "show_badges": True, "show_emotes": True,
        "show_timestamps": False, "fade_seconds": 30, "max_messages": 50,
    },
    "tabs": [],
    # jump_to_latest_when_idle: a chat scrolled up jumps back to the newest
    # messages after 15 s without activity in it (ChatView.IDLE_RESUME_MS).
    "general": {"restore_tabs": True, "jump_to_latest_when_idle": True},
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
        "zoom_in": "Ctrl+=",
        "zoom_out": "Ctrl+-",
        "zoom_reset": "Ctrl+0",
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
        # Set by load() when settings.json was damaged; shown once at start.
        self.load_problem = ""

    @classmethod
    def load(cls, config_dir: Path, *,
             now: Callable[[], dt.datetime] | None = None) -> Settings:
        """Read settings.json. A damaged file is never overwritten: it's kept as
        settings.json.corrupt-<time> and the last good copy (settings.json.bak)
        is loaded instead, or the defaults when that can't be read either."""
        path = config_dir / "settings.json"
        data: dict[str, Any] = {}
        problem = ""
        if path.exists():
            read = _read_json(path)
            if read is not None:
                data = read
            else:
                corrupt = _corrupt_name(path, (now or dt.datetime.now)())
                try:
                    path.replace(corrupt)
                except OSError as exc:
                    # Can't move it (e.g. open elsewhere on Windows): copy it
                    # aside so the next save can't destroy the only copy.
                    log.warning("could not move damaged %s aside (%s); copying it", path, exc)
                    try:
                        shutil.copyfile(path, corrupt)
                    except OSError as copy_exc:
                        log.warning("could not copy damaged %s: %s", path, copy_exc)
                backup = _read_json(path.with_name("settings.json.bak"))
                if backup is not None:
                    data, problem = backup, RESTORED_MESSAGE
                else:
                    problem = NO_BACKUP_MESSAGE
                log.warning("damaged %s kept as %s; %s", path, corrupt.name, problem)
        settings = cls(data, path)
        settings.load_problem = problem
        return settings

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
        # Keep the previous good file first: if the write below fails,
        # settings.json is untouched and the backup is already there.
        if _read_json(self._path) is not None:
            backup = self._path.with_name("settings.json.bak")
            backup_tmp = self._path.with_name("settings.json.bak.tmp")
            try:
                shutil.copyfile(self._path, backup_tmp)
                backup_tmp.replace(backup)
            except OSError as exc:  # a missing backup must never block saving
                log.warning("could not update %s: %s", backup, exc)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)


def _read_json(path: Path) -> dict[str, Any] | None:
    """The file's JSON object, or None when it's missing or damaged."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _corrupt_name(path: Path, when: dt.datetime) -> Path:
    """settings.json.corrupt-<YYYYmmdd-HHMMSS>, with -2, -3 … if taken."""
    base = f"settings.json.corrupt-{when.strftime('%Y%m%d-%H%M%S')}"
    candidate = path.with_name(base)
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{base}-{n}")
        n += 1
    return candidate
