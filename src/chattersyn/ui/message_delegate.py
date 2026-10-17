"""MessageDelegate: paints chat rows with cached QTextDocument layouts.

Each message gets one QTextDocument (cached per (message id, width)) with a
custom QTextObjectInterface for emote/badge images — images are resolved
lazily from the global ImageCache and rows repaint when images arrive.
Custom QTextFormat properties carry hit-test metadata (links, mentions,
username span) so clicks are routed without inline HTML or a widget per row.
The object replacement character (U+FFFC) is inserted as plain text since
PySide6 6.12 no longer exposes QChar.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QMovie,
    QPainter,
    QPixmap,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextObjectInterface,
)
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate

from ..config.settings import Settings
from ..core.image_cache import image_cache
from ..models.message import Message, MessageFlags, Platform, TextRun
from . import theme

# -- custom format property ids ------------------------------------------------

EMOTE_TYPE = QTextFormat.ObjectTypes.UserObject + 1
EMOTE_URL = QTextFormat.Property.UserProperty + 1
EMOTE_ALT = QTextFormat.Property.UserProperty + 2
EMOTE_ANIMATED = QTextFormat.Property.UserProperty + 3
EMOTE_HEIGHT = QTextFormat.Property.UserProperty + 4
MENTION = QTextFormat.Property.UserProperty + 5

CHIP_STYLE = {
    Platform.TWITCH: ("Tw", "#9146ff"),
    Platform.YOUTUBE: ("Yt", "#e62117"),
}
CHIP_W = 26
CHIP_H = 15
ROW_MARGIN_X = 6
ROW_MARGIN_Y = 3
BAR_W = 3

SUB_COLOR = QColor("#00b3ff")
ANNOUNCE_COLORS = {
    "blue": QColor("#1e90ff"),
    "green": QColor("#2ba640"),
    "orange": QColor("#ff8c1a"),
    "purple": QColor("#a970ff"),
}
SUPERCHAT_COLOR = QColor("#f6c344")
MEMBERSHIP_COLOR = QColor("#2ba640")
CHEER_COLOR = QColor("#9146ff")

# Moderation-mode action buttons (right gutter of eligible rows).
MOD_BUTTON_H = 16
MOD_BUTTON_GAP = 3
MOD_BUTTON_PAD_X = 6
MOD_FONT_PT = 8.0

# Author badges that exempt a row from moderation buttons (the broadcaster's
# own rows and moderators').
PROTECTED_TWITCH_BADGES = {"broadcaster", "moderator", "staff", "admin", "global_mod"}
PROTECTED_YT_BADGES = {"owner", "moderator"}  # YouTube badges: set_id "yt"


def _variant_value(fmt: QTextFormat, prop: int):
    """Read a custom format property; PySide6 may return a QVariant wrapper."""
    value = fmt.property(prop)
    if value is not None and type(value).__name__ == "QVariant":
        value = value.value()
    return value


class _EmoteInterface(QObject, QTextObjectInterface):
    """Draws emote/badge images (or a placeholder) inside a QTextDocument.

    PySide6 6.12's registerHandler requires a QObject, so the interface is
    implemented as a QObject subclass (Qt5-style combination class).
    """

    def intrinsicSize(self, doc, posInDocument, fmt):
        height = max(16.0, float(fmt.intProperty(EMOTE_HEIGHT) or 28))
        return QSize(height, height)

    def draw(self, painter, rect, doc, posInDocument, fmt):
        url = fmt.stringProperty(EMOTE_URL) or ""
        alt = fmt.stringProperty(EMOTE_ALT) or ""
        animated = fmt.boolProperty(EMOTE_ANIMATED)
        pix: QPixmap | None = None
        if url:
            obj = image_cache.request(url, animated)
            if isinstance(obj, QMovie):
                pix = obj.currentPixmap()
            elif isinstance(obj, QPixmap):
                pix = obj
        if pix is not None and not pix.isNull():
            target = rect.toRect().size()
            scaled = pix.scaled(target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            x = rect.x() + (rect.width() - scaled.width()) / 2
            y = rect.y() + (rect.height() - scaled.height()) / 2
            painter.drawPixmap(int(x), int(y), scaled)
        else:
            # Placeholder: rounded box with the emote/badge name (like Chatterino's
            # loading state; also used for fake-feed emotes without URLs).
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QColor("#4a4a4a"))
            painter.setBrush(QColor("#383838"))
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 3.0, 3.0)
            font = QFont()
            font.setPointSizeF(7.0)
            painter.setFont(font)
            painter.setPen(QColor("#9a9a9a"))
            painter.setClipRect(rect)
            painter.drawText(rect.adjusted(2, 0, -2, 0), Qt.AlignmentFlag.AlignCenter, alt)
            painter.restore()


_EMOTE_INTERFACE = _EmoteInterface()


@dataclass
class _RowLayout:
    doc: QTextDocument
    height: int
    ts_text: str = ""
    ts_width: int = 0
    uname_span: tuple[int, int] | None = None
    chip: tuple[str, str] | None = None
    urls: list[str] = field(default_factory=list)
    x_offset: int = 0  # where the document starts (after chip + margins)
    gutter: int = 0  # right-side reserve for moderation buttons
    # (x, y, w, h, label) relative to the row rect origin
    buttons: list[tuple[float, float, float, float, str]] = field(default_factory=list)


def _format_timestamp(ts, fmt: str) -> str:
    """Chatterino-style timestamp format tokens: HH, mm, ss, h, m."""
    tokens = [("HH", f"{ts.hour:02d}"), ("hh", f"{ts.hour % 12 or 12:02d}"),
              ("mm", f"{ts.minute:02d}"), ("ss", f"{ts.second:02d}")]
    for token, value in tokens:
        fmt = fmt.replace(token, value)
    return fmt


class MessageDelegate(QStyledItemDelegate):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._layouts: OrderedDict[tuple[str, int, str, int, int, str], _RowLayout] = OrderedDict()
        self._url_index: dict[str, set[str]] = {}
        self._max_layouts = 2048
        # Moderation mode (set by ChatView/MainWindow): part of the layout key.
        self._moderation_enabled = False
        self._moderation_actions: list[str] = []
        self._youtube_actions: list[str] = []  # fixed per-row actions on YouTube rows
        self._mod_version = 0
        theme.theme_notifier.changed.connect(self.clear_cache)
        image_cache.image_ready.connect(self._on_image_ready)

    # -- moderation mode ---------------------------------------------------------

    def set_moderation_enabled(self, enabled: bool) -> None:
        if enabled != self._moderation_enabled:
            self._moderation_enabled = enabled
            self._mod_version += 1
            self._refresh()

    def set_moderation_actions(self, actions: list[str]) -> None:
        if actions != self._moderation_actions:
            self._moderation_actions = list(actions)
            self._mod_version += 1

    def set_youtube_actions(self, actions: list[str]) -> None:
        """Per-row actions on YouTube rows (Ban/Timeout/Hide — docs/m4.md)."""
        if actions != self._youtube_actions:
            self._youtube_actions = list(actions)
            self._mod_version += 1
            self._refresh()

    def _refresh(self) -> None:
        view = self.parent()
        if view is not None:
            view.viewport().update()

    def _mod_buttons_for(self, msg: Message) -> list[str]:
        """Action labels for one row: moderation mode ON, regular messages
        only, skipping the broadcaster's own and moderators' rows. Twitch
        rows use the configured templates; YouTube rows the fixed set."""
        if not self._moderation_enabled:
            return []
        if msg.is_system or msg.extra.get("self"):
            return []
        if msg.author is None:
            return []
        for badge in msg.author.badges:
            if badge.set_id in PROTECTED_TWITCH_BADGES:
                return []
            if badge.set_id == "yt" and badge.version_id in PROTECTED_YT_BADGES:
                return []
        if msg.platform is Platform.YOUTUBE:
            return list(self._youtube_actions)
        return list(self._moderation_actions)

    # -- cache management --------------------------------------------------------

    def clear_cache(self) -> None:
        self._layouts.clear()
        self._url_index.clear()

    def invalidate_url(self, url: str) -> None:
        ids = self._url_index.pop(url, set())
        if not ids:
            return
        for key in [k for k in self._layouts if k[0] in ids]:
            self._layouts.pop(key, None)
        view = self.parent()
        if view is not None:
            view.viewport().update()

    def _on_image_ready(self, url: str) -> None:
        self.invalidate_url(url)

    def _cache(self, key: tuple[str, int, str, int, int, str], layout: _RowLayout) -> None:
        self._layouts[key] = layout
        self._layouts.move_to_end(key)
        for url in layout.urls:
            self._url_index.setdefault(url, set()).add(key[0])
        while len(self._layouts) > self._max_layouts:
            old_key, old = self._layouts.popitem(last=False)
            for url in old.urls:
                bucket = self._url_index.get(url)
                if bucket:
                    bucket.discard(old_key[0])
                    if not bucket:
                        del self._url_index[url]

    # -- layout building ---------------------------------------------------------

    def _layout_for(self, msg: Message, width: int, dpr: float) -> _RowLayout:
        # Flags, the moderation version AND the plain text are part of the
        # key: DELETED/REPLY/DISABLED change the document, and the "(N times)"
        # restack mutates msg.plain in place — a stale cached layout would
        # never repaint the row (dataChanged re-runs paint but _layout_for
        # would hand back the old document).
        key = (msg.id, width, f"{dpr:.1f}", int(msg.flags), self._mod_version, msg.plain)
        cached = self._layouts.get(key)
        if cached is not None:
            self._layouts.move_to_end(key)
            return cached
        layout = self._build_layout(msg, width, dpr)
        self._cache(key, layout)
        return layout

    def _build_layout(self, msg: Message, width: int, dpr: float) -> _RowLayout:
        palette = QApplication.instance().palette()
        app_font = QApplication.instance().font()

        body_font = QFont(app_font)
        body_font.setPointSizeF(max(9.5, app_font.pointSizeF() - 0.5))
        ts_font = QFont(app_font)
        ts_font.setPointSizeF(max(8.0, app_font.pointSizeF() - 2.0))

        ts_fmt = self._settings.get("timestamps.format", "HH:mm:ss")
        ts_text = _format_timestamp(msg.ts, ts_fmt) if self._settings.get("timestamps.enabled", True) else ""
        ts_width = int(QFontMetricsF(ts_font).horizontalAdvance(ts_text)) if ts_text else 0

        chip = CHIP_STYLE.get(msg.platform) if not msg.is_system else None
        chip_w = CHIP_W + 4 if chip else 0

        # Moderation buttons reserve a right gutter.
        buttons = self._mod_buttons_for(msg)
        gutter = 0
        button_rects: list[tuple[float, float, float, float, str]] = []
        if buttons:
            mod_font = QFont(app_font)
            mod_font.setPointSizeF(MOD_FONT_PT)
            metrics = QFontMetricsF(mod_font)
            label_w = int(max(metrics.horizontalAdvance(label) for label in buttons))
            gutter = label_w + 2 * MOD_BUTTON_PAD_X + 2
            bx = width - gutter + MOD_BUTTON_PAD_X
            by = ROW_MARGIN_Y + 3
            for label in buttons:
                bw = int(metrics.horizontalAdvance(label)) + 2 * MOD_BUTTON_PAD_X
                button_rects.append((float(bx), float(by), float(bw), float(MOD_BUTTON_H), label))
                by += MOD_BUTTON_H + MOD_BUTTON_GAP

        doc = QTextDocument()
        doc.setDocumentMargin(0)
        doc.documentLayout().registerHandler(EMOTE_TYPE, _EMOTE_INTERFACE)

        base = QTextCharFormat()
        base.setFont(body_font)
        base.setForeground(palette.color(palette.ColorRole.Text))

        deleted = bool(msg.flags & MessageFlags.DELETED)
        if deleted:
            base.setFontStrikeOut(True)

        cursor = QTextCursor(doc)
        block = QTextBlockFormat()
        block.setTopMargin(1)
        block.setBottomMargin(1)
        centered = bool(msg.flags & MessageFlags.CENTERED) or msg.is_system
        if centered:
            block.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        cursor.setBlockFormat(block)
        cursor.setCharFormat(base)

        urls: list[str] = []
        uname_span: tuple[int, int] | None = None

        if msg.author is None or centered:
            gray = QTextCharFormat(base)
            gray.setForeground(QColor("#8a8a8a"))
            cursor.insertText(msg.plain, gray)
        else:
            author = msg.author
            for badge in author.badges:
                fmt = QTextCharFormat(base)
                fmt.setObjectType(EMOTE_TYPE)
                fmt.setProperty(EMOTE_HEIGHT, 16)
                fmt.setProperty(EMOTE_URL, badge.url_for(dpr) or "")
                fmt.setProperty(EMOTE_ALT, badge.title or badge.set_id)
                fmt.setProperty(EMOTE_ANIMATED, False)
                fmt.setToolTip(badge.tooltip or badge.title or badge.set_id)
                cursor.insertText("\ufffc", fmt)

            name_fmt = QTextCharFormat(base)
            name_fmt.setFontWeight(QFont.Weight.Bold)
            name_color = QColor(author.color) if author.color else palette.color(palette.ColorRole.Text)
            name_fmt.setForeground(name_color)
            name_fmt.setProperty(MENTION, author.login)
            uname_start = cursor.position()
            cursor.insertText(author.name, name_fmt)
            uname_span = (uname_start, cursor.position())

            action = bool(msg.flags & MessageFlags.ACTION)
            sep = QTextCharFormat(base)
            if action:
                sep.setForeground(name_color)
                cursor.insertText(" ", sep)
            else:
                cursor.insertText(": ", base)

            if msg.flags & MessageFlags.REPLY and msg.reply_parent_name:
                reply_fmt = QTextCharFormat(base)
                reply_fmt.setForeground(QColor("#8a8a8a"))
                reply_fmt.setFontItalic(True)
                cursor.insertText(f"↳ @{msg.reply_parent_name} ", reply_fmt)

            for run in msg.runs:
                if isinstance(run, TextRun):
                    fmt = QTextCharFormat(base)
                    if action:
                        fmt.setForeground(name_color)
                    if run.link:
                        fmt.setAnchor(True)
                        fmt.setAnchorHref(run.link)
                        fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SingleUnderline)
                        fmt.setForeground(palette.color(palette.ColorRole.Link))
                        urls.append(run.link)
                    if run.mention:
                        fmt.setProperty(MENTION, run.mention)
                        fmt.setForeground(palette.color(palette.ColorRole.Link))
                        fmt.setFontWeight(QFont.Weight.Bold)
                    cursor.insertText(run.text, fmt)
                else:  # ImageRun
                    emote = run.emote
                    scale = "2x" if dpr >= 2.0 else "1x"
                    fmt = QTextCharFormat(base)
                    fmt.setObjectType(EMOTE_TYPE)
                    fmt.setProperty(EMOTE_HEIGHT, max(20, emote.height_px))
                    fmt.setProperty(EMOTE_URL, emote.url_for_scale(scale) or "")
                    fmt.setProperty(EMOTE_ALT, run.alt or emote.name)
                    fmt.setProperty(EMOTE_ANIMATED, emote.animated)
                    fmt.setToolTip(run.alt or emote.name)
                    cursor.insertText("\ufffc", fmt)
                    if emote.url_for_scale(scale):
                        urls.append(emote.url_for_scale(scale))

        reserved = ROW_MARGIN_X * 2 + chip_w + (ts_width + 8 if ts_width else 0) + gutter
        text_width = max(10, width - reserved)
        doc.setTextWidth(text_width)
        buttons_h = (len(buttons) * (MOD_BUTTON_H + MOD_BUTTON_GAP) - MOD_BUTTON_GAP
                     if buttons else 0)
        height = max(int(doc.size().height()) + ROW_MARGIN_Y * 2 + 4,
                     buttons_h + ROW_MARGIN_Y * 2 + 8)

        return _RowLayout(
            doc=doc,
            height=height,
            ts_text=ts_text,
            ts_width=ts_width,
            uname_span=uname_span,
            chip=chip,
            urls=urls,
            x_offset=ROW_MARGIN_X + chip_w,
            gutter=gutter,
            buttons=button_rects,
        )

    # -- Qt delegate API ----------------------------------------------------------

    def sizeHint(self, option, index):
        msg = index.data(index.model().MessageRole) if index.model() else None
        if msg is None:
            return QSize(0, 24)
        dpr = option.widget.devicePixelRatioF() if option.widget else 1.0
        layout = self._layout_for(msg, max(60, option.rect.width()), dpr)
        return QSize(0, layout.height)

    def paint(self, painter, option, index):
        msg = index.data(index.model().MessageRole) if index.model() else None
        if msg is None:
            return
        rect = option.rect
        dpr = option.widget.devicePixelRatioF() if option.widget else 1.0
        layout = self._layout_for(msg, max(60, rect.width()), dpr)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Timed-out / locally-hidden rows: grayed "disabled" overlay.
        disabled = bool(msg.flags & MessageFlags.DISABLED)
        if disabled:
            painter.setOpacity(0.45)

        # Row background by flags
        if option.state & QStyle.StateFlag.State_Selected:
            sel = QApplication.instance().palette().color(
                QApplication.instance().palette().ColorRole.Highlight)
            sel.setAlpha(110)
            painter.fillRect(rect, sel)
        if msg.flags & MessageFlags.HIGHLIGHTED:
            hl = QColor(self._highlight_color(msg))
            hl.setAlpha(46)
            painter.fillRect(rect, hl)
        bar: QColor | None = None
        if msg.flags & MessageFlags.SUBSCRIPTION:
            bar = SUB_COLOR
        elif msg.flags & MessageFlags.ANNOUNCEMENT:
            bar = ANNOUNCE_COLORS.get(msg.extra.get("color", ""), ANNOUNCE_COLORS["purple"])
        elif msg.flags & MessageFlags.SUPERCHAT:
            bar = SUPERCHAT_COLOR
        elif msg.flags & MessageFlags.MEMBERSHIP:
            bar = MEMBERSHIP_COLOR
        elif msg.flags & MessageFlags.CHEER:
            bar = CHEER_COLOR
        if bar is not None:
            tint = QColor(bar)
            tint.setAlpha(20)
            painter.fillRect(rect, tint)
            painter.fillRect(rect.x(), rect.y(), BAR_W, rect.height(), bar)

        # Platform chip
        if layout.chip:
            chip_text, chip_color = layout.chip
            chip_rect = QRectF(rect.x() + ROW_MARGIN_X, rect.y() + ROW_MARGIN_Y + 1, CHIP_W, CHIP_H)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(chip_color))
            painter.drawRoundedRect(chip_rect, 3.5, 3.5)
            chip_font = QFont()
            chip_font.setPointSizeF(7.0)
            chip_font.setBold(True)
            painter.setFont(chip_font)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(chip_rect, Qt.AlignmentFlag.AlignCenter, chip_text)

        # Message body
        painter.translate(rect.x() + layout.x_offset, rect.y() + ROW_MARGIN_Y + 2)
        layout.doc.drawContents(painter)

        # Timestamp right-aligned on the first line (left of the mod gutter)
        if layout.ts_text:
            ts_font = QFont(QApplication.instance().font())
            ts_font.setPointSizeF(max(8.0, ts_font.pointSizeF() - 2.0))
            painter.resetTransform()
            painter.setFont(ts_font)
            painter.setPen(QColor("#8a8a8a"))
            ts_rect = QRectF(rect.right() - layout.gutter - layout.ts_width - ROW_MARGIN_X - 2,
                             rect.y() + ROW_MARGIN_Y + 3,
                             layout.ts_width, 16)
            painter.drawText(ts_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, layout.ts_text)

        # Moderation action buttons in the right gutter
        if layout.buttons:
            mod_font = QFont(QApplication.instance().font())
            mod_font.setPointSizeF(MOD_FONT_PT)
            painter.resetTransform()
            painter.setFont(mod_font)
            for bx, by, bw, bh, label in layout.buttons:
                brect = QRectF(rect.x() + bx, rect.y() + by, bw, bh)
                painter.setPen(QColor("#555555"))
                painter.setBrush(QColor("#2d2d2d"))
                painter.drawRoundedRect(brect, 3.0, 3.0)
                painter.setPen(QColor("#dddddd"))
                painter.drawText(brect, Qt.AlignmentFlag.AlignCenter, label)

        if disabled:
            painter.setOpacity(1.0)
            # The veil must cover the row rect itself: reset the body
            # translation even when neither the timestamp nor the mod buttons
            # ran (no timestamps + no buttons leaves the transform active).
            painter.resetTransform()
            veil = QColor("#141414")
            veil.setAlpha(40)
            painter.fillRect(rect, veil)

        painter.restore()

    # -- hit testing ---------------------------------------------------------------

    def _highlight_color(self, msg: Message) -> str:
        # A matched highlight entry stores its own colour on the message;
        # mentions and the settings default fall back to the global colour.
        return str(
            msg.extra.get("highlight_color")
            or self._settings.get("highlight_color", "#7a5cff"))

    def hit_test(self, index, pos: QPointF, visual_rect) -> tuple[str, str] | None:
        """Map a viewport click to ("link"|"user"|"emote"|"mod", payload) or None."""
        if not index.isValid():
            return None
        model = index.model()
        msg = model.data(index, model.MessageRole)
        if msg is None:
            return None
        dpr = self.parent().devicePixelRatioF() if self.parent() else 1.0
        layout = self._layout_for(msg, max(60, visual_rect.width()), dpr)
        # Moderation buttons sit in the gutter, outside the document.
        rel = QPointF(pos.x() - visual_rect.x(), pos.y() - visual_rect.y())
        for bx, by, bw, bh, label in layout.buttons:
            if bx <= rel.x() <= bx + bw and by <= rel.y() <= by + bh:
                return ("mod", label)
        local = QPointF(pos.x() - visual_rect.x() - layout.x_offset,
                        pos.y() - visual_rect.y() - ROW_MARGIN_Y - 2)
        if local.x() < 0:
            return None
        char_pos = layout.doc.documentLayout().hitTest(local, Qt.HitTestAccuracy.ExactHit)
        if char_pos < 0:  # point past the text: no hit
            return None
        if layout.uname_span and layout.uname_span[0] <= char_pos < layout.uname_span[1]:
            return ("user", msg.author.login)
        cursor = QTextCursor(layout.doc)
        cursor.setPosition(char_pos)
        fmt = cursor.charFormat()
        href = fmt.anchorHref()
        if href:
            return ("link", href)
        mention = _variant_value(fmt, MENTION)
        if isinstance(mention, str) and mention:
            return ("user", mention)
        if fmt.objectType() == EMOTE_TYPE:
            alt = fmt.stringProperty(EMOTE_ALT)
            if alt:
                return ("emote", alt)
        return None
