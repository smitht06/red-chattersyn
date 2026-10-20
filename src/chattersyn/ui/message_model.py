"""MessageModel: deque-backed list model with batched appends and a scrollback cap."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSortFilterProxyModel, Qt

from ..models.message import Message, MessageFlags, Platform


class MessageModel(QAbstractListModel):
    MessageRole = Qt.ItemDataRole.UserRole + 1
    PlatformRole = Qt.ItemDataRole.UserRole + 2
    PlainRole = Qt.ItemDataRole.UserRole + 3
    IdRole = Qt.ItemDataRole.UserRole + 4

    def __init__(self, max_messages: int = 1000, parent=None):
        super().__init__(parent)
        self._messages: deque[Message] = deque()
        self._pending: list[Message] = []
        self.max_messages = max_messages

    # -- Qt model API ----------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._messages)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._messages):
            return None
        msg = self._messages[index.row()]
        if role == self.MessageRole:
            return msg
        if role == self.PlatformRole:
            return msg.platform
        if role == self.PlainRole:
            return msg.plain
        if role == self.IdRole:
            return msg.id
        return None

    # -- mutation ----------------------------------------------------------------

    def add(self, msg: Message) -> None:
        """Queue a message; call `flush` (typically from a 100 ms timer) to publish."""
        self._pending.append(msg)

    def flush(self) -> tuple[int, int]:
        """Publish queued messages. Returns (added, trimmed)."""
        n = len(self._pending)
        if not n:
            return 0, 0
        # Keep the last max_messages of (existing + pending): trim from the
        # front of the existing rows first, then drop the oldest pending ones.
        trim = max(0, len(self._messages) + n - self.max_messages)
        trim_existing = min(trim, len(self._messages))
        if trim_existing:
            self.beginRemoveRows(QModelIndex(), 0, trim_existing - 1)
            for _ in range(trim_existing):
                self._messages.popleft()
            self.endRemoveRows()
        del self._pending[: trim - trim_existing]
        if self._pending:
            first = len(self._messages)
            last = first + len(self._pending) - 1
            self.beginInsertRows(QModelIndex(), first, last)
            self._messages.extend(self._pending)
            self._pending.clear()
            self.endInsertRows()
        return n, trim

    def clear(self) -> None:
        self._pending.clear()
        self.beginResetModel()
        self._messages.clear()
        self.endResetModel()

    def message_at(self, row: int) -> Message | None:
        if 0 <= row < len(self._messages):
            return self._messages[row]
        return None

    def message_by_id(self, msg_id: str) -> Message | None:
        for msg in self._messages:
            if msg.id == msg_id:
                return msg
        return None

    def mark_deleted(self, msg_id: str) -> bool:
        """Flag a message as deleted (Twitch CLEARMSG); True if it was visible."""
        return self.update_message(msg_id, lambda m: setattr(
            m, "flags", m.flags | MessageFlags.DELETED))

    def update_message(self, msg_id: str, mutator) -> bool:
        """Apply `mutator(msg)` to a visible row and repaint it (stacking
        "(N times)", DISABLED gray-out, pinned rows, ...). True if applied."""
        # Pending rows aren't visible yet but flush with the mutation applied.
        for msg in self._pending:
            if msg.id == msg_id:
                mutator(msg)
                return True
        for row, msg in enumerate(self._messages):
            if msg.id == msg_id:
                mutator(msg)
                index = self.index(row)
                self.dataChanged.emit(index, index, [self.MessageRole])
                return True
        return False

    def update_flags_for_user(self, channel_id: str, login: str, flag: MessageFlags,
                              enabled: bool = True) -> int:
        """Set/clear `flag` on every visible row from one user; returns the
        number of rows touched."""
        touched = 0
        for msg in self._pending:
            if (msg.channel_id == channel_id and msg.author is not None
                    and msg.author.login.lower() == login.lower()):
                msg.flags = (msg.flags | flag) if enabled else (msg.flags & ~flag)
                touched += 1
        first = last = -1
        for row, msg in enumerate(self._messages):
            if (msg.channel_id == channel_id and msg.author is not None
                    and msg.author.login.lower() == login.lower()):
                msg.flags = (msg.flags | flag) if enabled else (msg.flags & ~flag)
                touched += 1
                if first < 0:
                    first = row
                last = row
        if first >= 0:
            self.dataChanged.emit(self.index(first), self.index(last), [self.MessageRole])
        return touched

    def update_flags_for_channel(self, channel_id: str, flag: MessageFlags,
                                 enabled: bool = True) -> int:
        """Set/clear `flag` on every visible row in a channel (full
        CLEARCHAT: the spec's "disable all" keeps rows visible but grayed).
        Returns the number of rows touched."""
        touched = 0
        for msg in self._pending:
            if msg.channel_id == channel_id:
                msg.flags = (msg.flags | flag) if enabled else (msg.flags & ~flag)
                touched += 1
        first = last = -1
        for row, msg in enumerate(self._messages):
            if msg.channel_id == channel_id:
                msg.flags = (msg.flags | flag) if enabled else (msg.flags & ~flag)
                touched += 1
                if first < 0:
                    first = row
                last = row
        if first >= 0:
            self.dataChanged.emit(self.index(first), self.index(last), [self.MessageRole])
        return touched

    def all_messages(self) -> list[Message]:
        return list(self._messages)

    def row_of(self, msg_id: str) -> int | None:
        """Visible row index for a message id (search "Go to message")."""
        for row, msg in enumerate(self._messages):
            if msg.id == msg_id:
                return row
        return None

    def platforms_in_view(self) -> set[Platform]:
        return {msg.platform for msg in self._messages}


class PlatformProxyModel(QSortFilterProxyModel):
    """Read-only view of a MessageModel filtered to one platform. Backs the
    side-by-side panes: both panes read the same canonical MessageModel, so
    mutations (delete/flag/clear) reflect in both automatically and no
    message state is duplicated."""

    MessageRole = MessageModel.MessageRole
    PlatformRole = MessageModel.PlatformRole
    PlainRole = MessageModel.PlainRole
    IdRole = MessageModel.IdRole

    def __init__(self, platform: Platform, source: MessageModel, parent=None):
        super().__init__(parent)
        self._platform = platform
        self.setSourceModel(source)

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        src = self.sourceModel()
        return src.data(src.index(row, 0, parent), self.PlatformRole) == self._platform

    def message_at(self, row: int) -> Message | None:
        """Mirror MessageModel.message_at for a proxy row (click/copy paths)."""
        return self.data(self.index(row, 0), self.MessageRole)

    def row_of(self, msg_id: str) -> int | None:
        """Proxy row for a message id (search "Go to message"); None if absent."""
        src = self.sourceModel()
        src_row = src.row_of(msg_id)
        if src_row is None:
            return None
        proxy = self.mapFromSource(src.index(src_row, 0))
        return proxy.row() if proxy.isValid() else None
