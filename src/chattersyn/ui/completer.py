"""AutoCompletePopup: the frameless completion list floating above the input.

DontFocus — keyboard events keep going to the QLineEdit, which drives the
popup (Up/Down/Enter/Tab/Escape). Rows: emote thumbnail or emoji glyph +
name, provider/user label right-aligned. Selection resets on every
set_candidates (every keystroke — parity).
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate

from ..core.image_cache import image_cache
from ..services.autocomplete import CompletionCandidate
from .popup import BasePopup

ROW_HEIGHT = 22
MAX_VISIBLE_ROWS = 10
POPUP_WIDTH = 280


class _CompletionModel(QAbstractListModel):
    SubRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[CompletionCandidate] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item.name
        if role == self.SubRole:
            return item.sub
        if role == Qt.ItemDataRole.DecorationRole:
            return item
        return None

    def set_items(self, items: list[CompletionCandidate]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()

    def item_at(self, row: int) -> CompletionCandidate | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None


class _CompletionDelegate(QStyledItemDelegate):
    """Pixmap (or emoji glyph) + name, provider label right-aligned."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._font = QFont()
        self._font.setPointSizeF(9.5)

    def sizeHint(self, option, index) -> QSize:
        return QSize(POPUP_WIDTH, ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        item = index.data(Qt.ItemDataRole.DecorationRole)
        if item is None:
            return
        rect = option.rect.adjusted(6, 1, -6, -1)
        painter.setFont(self._font)

        if item.glyph:
            painter.setPen(option.palette.color(option.palette.ColorRole.Text))
            painter.drawText(QRect(rect.x(), rect.y(), 22, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter, item.glyph)
            rect.setLeft(rect.left() + 24)
        elif item.emote is not None and item.emote.url:
            pixmap = image_cache.request(item.emote.url, False)
            if isinstance(pixmap, QPixmap) and not pixmap.isNull():
                scaled = pixmap.scaled(
                    20, 20, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap(rect.x(), rect.y() + (rect.height() - 20) // 2, scaled)
            rect.setLeft(rect.left() + 24)

        painter.setPen(option.palette.color(option.palette.ColorRole.Text))
        metrics = QFontMetrics(self._font)
        sub = item.sub
        if sub:
            sub_width = metrics.horizontalAdvance(sub)
            name_rect = QRect(rect.x(), rect.y(), rect.width() - sub_width - 8, rect.height())
            elided = metrics.elidedText(item.name, Qt.TextElideMode.ElideRight, name_rect.width())
            painter.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter, elided)
            painter.setPen(option.palette.color(option.palette.ColorRole.PlaceholderText))
            painter.drawText(QRect(rect.right() - sub_width, rect.y(), sub_width, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, sub)
        else:
            elided = metrics.elidedText(item.name, Qt.TextElideMode.ElideRight, rect.width())
            painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter, elided)


class AutoCompletePopup(BasePopup):
    """Completion list; the owner (InputWidget) drives selection/acceptance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowDoesNotAcceptFocus)
        self._model = _CompletionModel(self)
        self._view = QListView(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(_CompletionDelegate(self._view))
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._view.setFrameShape(self._view.Shape.NoFrame)
        self._view.setFixedWidth(POPUP_WIDTH)
        self._start = 0
        self._end = 0
        image_cache.image_ready.connect(self._on_image_ready)

    # -- API ----------------------------------------------------------------------

    def set_candidates(self, candidates: list[CompletionCandidate],
                       start: int, end: int) -> None:
        """New candidate list for the typed word [start, end); selection resets."""
        self._start, self._end = start, end
        self._model.set_items(candidates)
        self._view.setFixedHeight(min(len(candidates), MAX_VISIBLE_ROWS) * ROW_HEIGHT + 4)
        if candidates:
            self._view.setCurrentIndex(self._model.index(0, 0))
        self.adjustSize()

    def replace_range(self) -> tuple[int, int]:
        return self._start, self._end

    def current(self) -> CompletionCandidate | None:
        return self._model.item_at(self._view.currentIndex().row())

    def move_selection(self, delta: int) -> None:
        """Up/Down with wrap-around (parity)."""
        count = self._model.rowCount()
        if not count:
            return
        row = (self._view.currentIndex().row() + delta) % count
        self._view.setCurrentIndex(self._model.index(row, 0))
        self._view.scrollTo(self._view.currentIndex())

    def show_above(self, anchor) -> None:
        """Floating just above the input, left-aligned to it."""
        self.adjustSize()
        pos = anchor.mapToGlobal(QPoint(0, 0))
        pos -= QPoint(0, self.height() + 2)
        self.move(pos)
        self.show()

    # -- internals -------------------------------------------------------------------

    def _on_image_ready(self, url: str) -> None:
        # Emote thumbnails arrive asynchronously; repaint whatever is visible.
        self._view.viewport().update()
