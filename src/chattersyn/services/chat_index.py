"""ChatIndex: per-channel chatter snapshots for '@' autocomplete.

Fed by the Bridge: message authors (both platforms — for YouTube the channel
key is the live chat id) and the IRC Names (353) event. Each channel keeps a
bounded, deduplicated list of names in first-seen order — the 353 chatters
snapshot arrives on join and comes first, message authors follow. Display-name
casing from messages replaces a lowercase 353 entry.
"""

from __future__ import annotations

import logging

from ..core.bus import bridge
from ..models.message import Message
from ..services.twitch.mapper import Names

log = logging.getLogger(__name__)

MAX_USERS = 2000


class ChatIndex:
    """Ingress recorder: channel id -> ordered unique chatter names."""

    def __init__(self, max_users: int = MAX_USERS):
        self._max_users = max_users
        self._channels: dict[str, dict[str, None]] = {}  # channel -> lower login -> None
        self._connected = False

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        if self._connected:
            return
        bridge.message.connect(self._on_message)
        bridge.event.connect(self._on_event)
        self._connected = True

    def stop(self) -> None:
        if not self._connected:
            return
        bridge.message.disconnect(self._on_message)
        bridge.event.disconnect(self._on_event)
        self._connected = False

    # -- recording -------------------------------------------------------------------

    def _on_message(self, msg: Message) -> None:
        if msg.author is None or not msg.channel_id:
            return
        self.add_user(msg.channel_id, msg.author.name or msg.author.login)

    def _on_event(self, event) -> None:
        if isinstance(event, Names):
            for name in event.names:
                self.add_user(event.channel, name)

    def add_user(self, channel: str, name: str) -> None:
        name = name.strip()
        if not channel or not name:
            return
        names = self._channels.setdefault(channel, {})
        key = name.lower()
        if key in names:
            if names[key] != name and name != key:
                names[key] = name  # messages carry display-name casing — prefer it
            return
        names[key] = name
        while len(names) > self._max_users:
            names.pop(next(iter(names)))  # drop the oldest

    # -- queries ----------------------------------------------------------------------

    def users(self, channel: str) -> list[str]:
        """Names for a channel in first-seen order (or empty)."""
        return list(self._channels.get(channel, {}).values())
