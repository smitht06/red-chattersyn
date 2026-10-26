"""UsercardPopup: Chatterino-style user card on username click.

Opens at the cursor with the fields known from the message (Author data),
then gets Helix/YouTube enrichment pushed in asynchronously by MainWindow
(set_details): account age, follower/subscriber count, followage. No chat
badges are rendered (parity). The recent-messages section scans the tab's
local scrollback and grows live while the popup is open; moderation is
BUTTONS, not a menu — Ban/Unban + a timeout duration strip + the custom
moderation_actions templates (Twitch), Ban/Unban/Timeout 5m/Delete (YouTube),
visible only with mod rights and target != self.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import Settings
from ..core.image_cache import image_cache
from ..models.message import Message, Platform
from .message_delegate import MessageDelegate
from .popup import BasePopup
from .search_popup import ResultsModel

_TIMEOUT_STRIP = ("1s", "30s", "1m", "5m", "30m", "1h", "1d", "1w")

CARD_WIDTH = 360
AVATAR_SIZE = 64
TIMEOUT_COLUMNS = 4  # the 8-step strip wraps into two even rows
ACTION_COLUMNS = 3   # custom moderation_actions grid

_SECTION_STYLE = "color: #b0a8e8; font-weight: bold;"
_KEY_STYLE = "color: #8a8a8a;"
# The popup is always dark (popup.py), so the buttons are too.
_MOD_STYLE = (
    "QPushButton { background: #2d2d2d; color: #dddddd; border: 1px solid #444444;"
    " border-radius: 4px; padding: 4px 6px; }"
    "QPushButton:hover { background: #3a3a3a; border-color: #5c5c5c; }"
    "QPushButton:pressed { background: #262626; }"
    "QPushButton[danger=\"true\"] { background: #4a1f22; border-color: #7a3036; color: #ffd6d9; }"
    "QPushButton[danger=\"true\"]:hover { background: #5c262a; }"
)


@dataclass
class UsercardTarget:
    platform: Platform
    login: str
    display_name: str
    user_id: str = ""
    avatar_url: str | None = None
    color: str | None = None
    is_self: bool = False  # mod buttons never target yourself
    channel_id: str = ""  # chat the card's row came from (YouTube: the lcid)


def recent_messages_for(messages: list[Message], login: str) -> list[Message]:
    """Messages by `login` (ci) plus system rows mentioning them (sub-message
    prefixes, timeout rows). Whispers don't exist in the merged model."""
    needle = login.lower()
    return [
        msg for msg in messages
        if (msg.author is not None and (msg.author.login or "").lower() == needle)
        or (msg.author is None and needle in msg.plain.lower())
    ]


def _duration_text(created_at: str) -> str:
    """Human age for the Created row's tooltip."""
    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    days = max(0, (dt.datetime.now(dt.UTC) - created).days)
    if days >= 730:
        return f"{days // 365} years"
    if days >= 60:
        return f"{days // 30} months"
    return f"{days} days"


def _circle_pixmap(source: QPixmap, size: int) -> QPixmap:
    """`source` scaled to fill a `size` circle (Twitch-style round avatar)."""
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    scaled = source.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addEllipse(QRectF(0, 0, size, size))
    painter.setClipPath(path)
    painter.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return out


def _initial_avatar(name: str, color: str | None, size: int) -> QPixmap:
    """Placeholder until (or instead of) the real avatar: the name's initial
    on a disc in the user's chat colour."""
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    disc = QColor(color or "#5a5a5a")
    disc.setAlpha(170)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(disc)
    painter.drawEllipse(0, 0, size, size)
    font = QFont()
    font.setPixelSize(size // 2)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(out.rect(), Qt.AlignmentFlag.AlignCenter, (name[:1] or "?").upper())
    painter.end()
    return out


class UsercardPopup(BasePopup):
    action_requested = Signal(str)  # moderation command text (placeholders expanded)

    def __init__(self, settings: Settings, target: UsercardTarget,
                 recent_entries: list[tuple[object, Message]], *,
                 self_is_mod: bool = False,
                 custom_actions: list[dict] | None = None, parent=None):
        super().__init__(parent)
        self.target = target
        self._avatar_url = ""
        # Fixed width, content height: the card grows as enrichment rows
        # arrive instead of leaving a fixed-size box half empty.
        self.setFixedWidth(CARD_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # -- header: avatar + identity -----------------------------------------
        header = QHBoxLayout()
        header.setSpacing(12)
        self._avatar = QPushButton(self)
        self._avatar.setFixedSize(AVATAR_SIZE, AVATAR_SIZE)
        self._avatar.setIconSize(QSize(AVATAR_SIZE, AVATAR_SIZE))
        self._avatar.setCursor(Qt.CursorShape.PointingHandCursor)
        self._avatar.setStyleSheet("QPushButton { border: none; background: transparent; }")
        self._avatar.setToolTip("Open channel page")
        self._avatar.setIcon(QIcon(_initial_avatar(
            target.display_name or target.login, target.color, AVATAR_SIZE)))
        self._avatar.clicked.connect(self._open_profile)
        header.addWidget(self._avatar, 0, Qt.AlignmentFlag.AlignTop)

        identity = QVBoxLayout()
        identity.setSpacing(2)
        identity.addStretch(1)
        self._name_label = QLabel(target.display_name or target.login, self)
        self._name_label.setStyleSheet(
            f"color: {target.color or '#e8e8e8'}; font-size: 13pt; font-weight: bold;")
        self._login_label = QLabel(f"@{target.login}", self)
        self._login_label.setStyleSheet(_KEY_STYLE)
        identity.addWidget(self._name_label)
        identity.addWidget(self._login_label)
        identity.addStretch(1)
        header.addLayout(identity, 1)
        layout.addLayout(header)

        # -- detail rows (hidden until data exists) ------------------------------
        details = QVBoxLayout()
        details.setSpacing(3)
        layout.addLayout(details)
        self._details_layout = details
        self._id_row, self._id_value = self._add_row("ID:")
        self._id_value.setText(target.user_id or "—")
        self._id_row.show()  # always visible; anonymous rows show "—"
        self._created_row, self._created_value = self._add_row("Created:")
        self._followers_row, self._followers_value = self._add_row(
            "Subscribers:" if target.platform is Platform.YOUTUBE else "Followers:")
        self._followage_row, self._followage_value = self._add_row("Follows you since:")

        layout.addWidget(self._divider())

        # -- recent messages ------------------------------------------------------
        self._recent_label = QLabel("Recent messages", self)
        self._recent_label.setStyleSheet(_SECTION_STYLE)
        layout.addWidget(self._recent_label)
        self._recent = QListView(self)
        self._recent.setModel(ResultsModel(self._recent))
        self._recent.setItemDelegate(MessageDelegate(settings, self._recent))
        self._recent.setSelectionMode(QListView.SelectionMode.NoSelection)
        self._recent.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._recent.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._recent.setFixedHeight(150)
        layout.addWidget(self._recent)

        # -- moderation: buttons, not a menu ---------------------------------------
        # Grouped grids (Ban/Unban, the timeout strip, custom actions) so every
        # label stays readable — one row of ~11 buttons crushed them.
        self._mod_frame = QWidget(self)
        self._mod_frame.setStyleSheet(_MOD_STYLE)
        self._mod_layout = QVBoxLayout(self._mod_frame)
        self._mod_layout.setContentsMargins(0, 0, 0, 0)
        self._mod_layout.setSpacing(6)
        self._mod_layout.addWidget(self._divider())
        moderation_label = QLabel("Moderation", self._mod_frame)
        moderation_label.setStyleSheet(_SECTION_STYLE)
        self._mod_layout.addWidget(moderation_label)
        self._build_mod_buttons(custom_actions or [])
        layout.addWidget(self._mod_frame)
        self._set_mod_visible(self_is_mod and not target.is_self)

        if recent_entries:
            self._recent.model().set_results(recent_entries)
        self._update_recent_label()

        image_cache.image_ready.connect(self._on_image_ready)
        if target.avatar_url:
            self._load_avatar(target.avatar_url)

    # -- API ----------------------------------------------------------------------

    def set_details(self, *, created_at: str = "", followers: int | None = None,
                    followage: str = "", avatar_url: str | None = None) -> None:
        """Async enrichment from MainWindow (Helix / YouTube channels.list)."""
        if avatar_url and avatar_url != self._avatar_url:
            self._load_avatar(avatar_url)
        if created_at:
            self._created_value.setText(created_at[:10])
            duration = _duration_text(created_at)
            if duration:
                self._created_value.setToolTip(f"Channel created {duration} ago")
            self._created_row.show()
        if followers is not None:
            self._followers_value.setText(f"{followers:,}")
            self._followers_row.show()
        if followage:
            self._followage_value.setText(followage[:10])
            self._followage_row.show()
        if self.isVisible():
            self.adjustSize()  # rows were added: grow, and stay on screen
            self._clamp_to_screen(self.pos())

    def append_recent(self, entry: tuple[object, Message]) -> None:
        """Live append for messages from this user while the popup is open."""
        model = self._recent.model()
        model.insert_entry(model.rowCount(), entry)
        self._update_recent_label()
        self._recent.scrollToBottom()

    def show_at_cursor(self) -> None:
        """Open at the cursor (parity), clamped to the screen it sits on."""
        self.adjustSize()
        self._clamp_to_screen(QCursor.pos() + QPoint(8, 8))
        self.show()
        self.raise_()

    def _clamp_to_screen(self, point: QPoint) -> None:
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        point.setX(max(geo.left(), min(point.x(), geo.right() - self.width() + 1)))
        point.setY(max(geo.top(), min(point.y(), geo.bottom() - self.height() + 1)))
        self.move(point)

    # -- internals ------------------------------------------------------------------

    def _add_row(self, label_text: str) -> tuple[QWidget, QLabel]:
        row = QWidget(self)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        key = QLabel(label_text, row)
        key.setStyleSheet(_KEY_STYLE)
        # One key column for every row, sized to the longest label.
        key.setFixedWidth(key.fontMetrics().horizontalAdvance("Follows you since:") + 6)
        value = QLabel("", row)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(key)
        lay.addWidget(value, 1)
        row.hide()
        self._details_layout.addWidget(row)
        return row, value

    def _divider(self) -> QFrame:
        line = QFrame(self)
        line.setFixedHeight(1)
        line.setStyleSheet("background: #333333; border: none;")
        return line

    def _build_mod_buttons(self, custom_actions: list[dict]) -> None:
        twitch = self.target.platform is Platform.TWITCH
        login = self.target.login
        self._mod_buttons: list[QPushButton] = []
        if twitch:
            self._add_button_grid([("Ban", f"/ban {login}", True),
                                   ("Unban", f"/unban {login}", False)], 2)
            self._add_caption("Timeout")
            self._add_button_grid(
                [(duration, f"/timeout {login} {duration}", False) for duration in _TIMEOUT_STRIP],
                TIMEOUT_COLUMNS)
            actions = []
            for entry in custom_actions:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                template = str(entry.get("template") or "")
                text = (template.replace("{user.name}", self.target.display_name)
                               .replace("{user.login}", login))
                actions.append((str(entry["name"]), text, False))
            if actions:
                self._add_caption("Actions")
                self._add_button_grid(actions, ACTION_COLUMNS)
        else:
            # YouTube: server ban + local hide for delete + fixed 300s timeout
            # (milestone decision — docs/m4.md).
            self._add_button_grid([("Ban", f"/ban {login}", True),
                                   ("Unban", f"/unban {login}", False),
                                   ("Timeout 5m", f"/timeout {login} 300", False),
                                   ("Delete", f"/delete {login}", False)], 2)

    def _add_caption(self, text: str) -> None:
        caption = QLabel(text, self._mod_frame)
        caption.setStyleSheet(_KEY_STYLE)
        self._mod_layout.addWidget(caption)

    def _add_button_grid(self, buttons: list[tuple[str, str, bool]], columns: int) -> None:
        """Equal-width buttons, `columns` per row: (label, command, danger)."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        for i, (label, text, danger) in enumerate(buttons):
            button = QPushButton(label, self._mod_frame)
            button.setToolTip(text)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if danger:
                button.setProperty("danger", True)
            button.clicked.connect(lambda checked=False, t=text: self.action_requested.emit(t))
            grid.addWidget(button, i // columns, i % columns)
            self._mod_buttons.append(button)
        self._mod_layout.addLayout(grid)

    def _set_mod_visible(self, visible: bool) -> None:
        self._mod_frame.setVisible(visible)

    def _open_profile(self) -> None:
        if self.target.platform is Platform.YOUTUBE:
            url = f"https://www.youtube.com/@{self.target.login}"
        else:
            url = f"https://www.twitch.tv/{self.target.login}"
        QDesktopServices.openUrl(QUrl(url))

    def _update_recent_label(self) -> None:
        count = self._recent.model().rowCount()
        self._recent_label.setText(
            f"Recent messages ({count})" if count else "No recent messages")

    # -- avatar ----------------------------------------------------------------------

    def _load_avatar(self, url: str) -> None:
        self._avatar_url = url
        obj = image_cache.request(url, False)
        if isinstance(obj, QPixmap):
            self._set_avatar(obj)

    def _on_image_ready(self, url: str) -> None:
        if url != self._avatar_url:
            return
        obj = image_cache.request(url, False)
        if isinstance(obj, QPixmap):
            self._set_avatar(obj)

    def _set_avatar(self, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        self._avatar.setIcon(QIcon(_circle_pixmap(pixmap, AVATAR_SIZE)))
