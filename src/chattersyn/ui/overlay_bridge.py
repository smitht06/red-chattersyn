"""Feeds one tab's chat (its MessageModel) to the OBS browser source.

Rows are published when the model inserts them, so everything the tab shows
counts — including your own optimistic rows, which never pass bridge.message.
A row that turns DELETED or DISABLED (a mod delete, a timeout, a ban, a full
clear) is removed from the page, by the id the page knows it under: own rows
get Twitch's real id later, in place, so ids are remembered per row object.
An un-timeout clears DISABLED, but a removed row never comes back.
"""

from __future__ import annotations

import collections
import contextlib
from collections.abc import Callable

from PySide6.QtCore import QModelIndex

from ..models.message import Message, MessageFlags
from ..services.overlay_feed import message_to_event
from .message_model import MessageModel

REMEMBER = 400  # rows whose page id is kept (the page shows at most 200)


class OverlayBridge:
    def __init__(self, model: MessageModel,
                 publish_message: Callable[[dict], None],
                 publish_remove: Callable[[list[str]], None]):
        self.model = model
        self._publish_message = publish_message
        self._publish_remove = publish_remove
        # id(Message) -> (the Message, its id on the page); the object is kept
        # so id() can't be reused while the entry lives.
        self._shown: collections.OrderedDict[int, tuple[Message, str]] = collections.OrderedDict()
        model.rowsInserted.connect(self._on_rows_inserted)
        model.dataChanged.connect(self._on_data_changed)

    def detach(self) -> None:
        for signal, slot in ((self.model.rowsInserted, self._on_rows_inserted),
                             (self.model.dataChanged, self._on_data_changed)):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect(slot)
        self._shown.clear()

    def _on_rows_inserted(self, parent: QModelIndex, first: int, last: int) -> None:
        for row in range(first, last + 1):
            msg = self.model.message_at(row)
            if msg is None:
                continue
            event = message_to_event(msg)
            if event is None:
                continue
            self._shown[id(msg)] = (msg, event["id"])
            while len(self._shown) > REMEMBER:
                self._shown.popitem(last=False)
            self._publish_message(event)

    def _on_data_changed(self, top: QModelIndex, bottom: QModelIndex, roles=None) -> None:
        gone: list[str] = []
        for row in range(top.row(), bottom.row() + 1):
            msg = self.model.message_at(row)
            if msg is None or not msg.flags & (MessageFlags.DELETED | MessageFlags.DISABLED):
                continue
            entry = self._shown.pop(id(msg), None)
            if entry is not None and entry[0] is msg:
                gone.append(entry[1])
        if gone:
            self._publish_remove(gone)
