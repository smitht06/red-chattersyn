"""EmotePickerPopup: five-tab picker (Favourite, Subs, Channel, Global,
Emojis) with a search box that hides the tabs while non-empty.

Clicking inserts (emote name / emoji `:shortcode:`). Favourites: Ctrl+click
toggles, Ctrl+Shift+click toggles+inserts, right-click offers a checkable
action; persisted as two names-only lists in the settings `favourites` key
(emotes + emojis). Unresolved favourites render as plain names under a
notice. Emoji rows group under category headers (Component last).
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QTabBar,
    QVBoxLayout,
)

from ..config.settings import Settings
from ..core.image_cache import image_cache
from ..models.emote import Emote
from ..services.emojis import CATEGORY_ORDER, EmojiEntry, emoji_dataset
from ..services.emotes.emote_registry import EmoteRegistry
from .popup import BasePopup

_SEARCH_CAP = 200

TABS = ("Favourite", "Subs", "Channel", "Global", "Emojis")

_KIND_EMOTE = "emote"
_KIND_EMOJI = "emoji"


class _PickerList(QListWidget):
    """Left-click only (right-click is the favourites context menu)."""

    item_left_clicked = Signal(object)  # QListWidgetItem

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                self.item_left_clicked.emit(item)


class EmotePickerPopup(BasePopup):
    insert_requested = Signal(str)  # text to insert at the input cursor

    def __init__(self, settings: Settings, registry: EmoteRegistry,
                 default_tab: str = "Subs", parent=None):
        super().__init__(parent)
        self._settings = settings
        self._registry = registry
        self._url_items: dict[str, list[QListWidgetItem]] = {}
        self.setFixedSize(320, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Search emotes and emojis…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search)

        self._tabs = QTabBar(self)
        self._tabs.setExpanding(False)
        for name in TABS:
            self._tabs.addTab(name)
        self._tabs.setCurrentIndex(max(TABS.index(default_tab), 0))
        self._tabs.currentChanged.connect(self._repopulate)
        layout.addWidget(self._tabs)

        self._list = _PickerList(self)
        self._list.setUniformItemSizes(True)
        self._list.item_left_clicked.connect(self._on_item_left_clicked)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._list, 1)

        image_cache.image_ready.connect(self._on_image_ready)
        self._repopulate()

    # -- population ---------------------------------------------------------------

    def _repopulate(self, *_args) -> None:
        self._list.clear()
        self._url_items.clear()
        if self._search.text().strip():
            self._tabs.hide()
            self._populate_search(self._search.text().strip())
            return
        self._tabs.show()
        tab = TABS[self._tabs.currentIndex()]
        if tab == "Favourite":
            self._populate_favourites()
        elif tab == "Subs":
            self._populate_emotes(self._registry.sub_emotes())
        elif tab == "Channel":
            self._populate_emotes(self._registry.channel_emotes())
        elif tab == "Global":
            self._populate_emotes(self._registry.global_emotes())
        else:
            self._populate_emojis()

    def _populate_emotes(self, emotes: list[Emote]) -> None:
        for emote in sorted(emotes, key=lambda e: e.name.lower()):
            self._add_emote_item(emote.name, emote)

    def _populate_emojis(self) -> None:
        entries = emoji_dataset()
        for category in CATEGORY_ORDER:
            group = sorted(
                (e for e in entries if e.category == category),
                key=lambda e: e.shortcode)
            if not group:
                continue
            self._add_header(category)
            for entry in group:
                self._add_emoji_item(entry)

    def _populate_favourites(self) -> None:
        favourites = self._favourites()
        by_name = {e.name.lower(): e for e in self._registry.all_emotes()}
        emoji_by_code = {e.shortcode: e for e in emoji_dataset()}
        resolved = [(name, emote) for name in favourites["emotes"]
                    if (emote := by_name.get(name.lower())) is not None]
        unresolved = [name for name in favourites["emotes"]
                      if name.lower() not in by_name]
        if resolved:
            self._add_header("Favourite emotes")
            for name, emote in sorted(resolved, key=lambda pair: pair[0].lower()):
                self._add_emote_item(name, emote)
        emojis = [emoji_by_code[code] for code in favourites["emojis"]
                  if code in emoji_by_code]
        if emojis:
            self._add_header("Favourite emojis")
            for entry in sorted(emojis, key=lambda e: e.shortcode):
                self._add_emoji_item(entry)
        if unresolved:
            self._add_header("Unresolved favourites (provider not loaded)")
            for name in sorted(unresolved, key=str.lower):
                item = QListWidgetItem(name)
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setForeground(Qt.GlobalColor.gray)
                self._list.addItem(item)
        if not resolved and not emojis and not unresolved:
            self._add_header("Nothing favourited yet — Ctrl+click an emote to add it")

    def _populate_search(self, query: str) -> None:
        needle = query.lower()
        emotes = sorted(
            (e for e in self._registry.all_emotes() if needle in e.name.lower()),
            key=lambda e: e.name.lower())[:_SEARCH_CAP]
        room = _SEARCH_CAP - len(emotes)
        for emote in emotes:
            self._add_emote_item(emote.name, emote)
        for entry in sorted(
                (e for e in emoji_dataset() if needle in e.shortcode.lower()),
                key=lambda e: e.shortcode.lower())[:room]:
            self._add_emoji_item(entry)

    # -- items ----------------------------------------------------------------------

    def _add_header(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(Qt.GlobalColor.gray)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self._list.addItem(item)

    def _add_emote_item(self, name: str, emote: Emote) -> None:
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, (_KIND_EMOTE, name, emote))
        item.setToolTip(f"{name} — {emote.provider}"
                        + (" (zero-width)" if emote.zero_width else ""))
        self._set_emote_icon(item, emote)
        self._list.addItem(item)

    def _add_emoji_item(self, entry: EmojiEntry) -> None:
        item = QListWidgetItem(f"{entry.char}  :{entry.shortcode}:")
        item.setData(Qt.ItemDataRole.UserRole, (_KIND_EMOJI, entry.shortcode, entry))
        self._list.addItem(item)

    def _set_emote_icon(self, item: QListWidgetItem, emote: Emote) -> None:
        url = emote.url
        if not url:
            return
        pixmap = image_cache.request(url, False)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            item.setIcon(QIcon(pixmap.scaled(24, 24, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)))
        else:
            self._url_items.setdefault(url, []).append(item)

    def _on_image_ready(self, url: str) -> None:
        items = self._url_items.pop(url, [])
        if not items:
            return
        pixmap = image_cache.request(url, False)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            icon = QIcon(pixmap.scaled(24, 24, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))
            for item in items:
                item.setIcon(icon)

    # -- placement ------------------------------------------------------------------------

    def show_above(self, anchor) -> None:
        """Floating above the input bar, left-aligned to it."""
        self.adjustSize()
        pos = anchor.mapToGlobal(QPoint(0, 0))
        pos -= QPoint(0, self.height() + 2)
        self.move(pos)
        self.show()
        self.raise_()

    # -- interaction ------------------------------------------------------------------

    def _on_item_left_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return  # header / unresolved row
        kind, name, _payload = data
        modifiers = QGuiApplication.keyboardModifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if ctrl:
            self._toggle_favourite(kind, name)
            if shift:
                self._insert(kind, name)
        else:
            self._insert(kind, name)

    def _insert(self, kind: str, name: str) -> None:
        self.insert_requested.emit(name if kind == _KIND_EMOTE else f":{name}:")

    def _toggle_favourite(self, kind: str, name: str) -> None:
        favourites = self._favourites()
        bucket = "emotes" if kind == _KIND_EMOTE else "emojis"
        if name in favourites[bucket]:
            favourites[bucket].remove(name)
        else:
            favourites[bucket].append(name)
        self._settings.set("favourites", favourites)
        self._settings.save()
        if self._search.text().strip() == "" and TABS[self._tabs.currentIndex()] == "Favourite":
            self._repopulate()

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self._list.itemAt(pos)
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not data:
            return
        kind, name, _payload = data
        bucket = "emotes" if kind == _KIND_EMOTE else "emojis"
        menu = QMenu(self)
        action = menu.addAction("Favourite")
        action.setCheckable(True)
        action.setChecked(name in self._favourites()[bucket])
        chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
        if chosen is not None:
            self._toggle_favourite(kind, name)

    # -- search -------------------------------------------------------------------------

    def _on_search_changed(self, _text: str) -> None:
        self._repopulate()

    # -- keyboard -------------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        key = event.key()
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if key == Qt.Key.Key_Escape:
            self.close()
            return
        if ctrl and key == Qt.Key.Key_F:
            self._search.setFocus()
            self._search.selectAll()
            return
        if ctrl and key == Qt.Key.Key_Tab:
            count = self._tabs.count()
            self._tabs.setCurrentIndex((self._tabs.currentIndex() + 1) % count)
            return
        if ctrl and Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            index = key - Qt.Key.Key_1
            if index < self._tabs.count():
                self._tabs.setCurrentIndex(index)
            return
        if key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            bar = self._list.verticalScrollBar()
            bar.setValue(bar.value() + (-bar.pageStep() if key == Qt.Key.Key_PageUp
                                        else bar.pageStep()))
            return
        super().keyPressEvent(event)

    # -- favourites ------------------------------------------------------------------------

    def _favourites(self) -> dict[str, list[str]]:
        raw = self._settings.get("favourites", {"emotes": [], "emojis": []})
        if isinstance(raw, list):  # legacy single-list shape = emotes
            raw = {"emotes": raw, "emojis": []}
        if not isinstance(raw, dict):
            raw = {"emotes": [], "emojis": []}
        return {"emotes": [str(n) for n in raw.get("emotes", [])],
                "emojis": [str(n) for n in raw.get("emojis", [])]}
