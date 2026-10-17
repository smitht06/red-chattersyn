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

from PySide6.QtCore import QPoint, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QCursor, QDesktopServices, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
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


class UsercardPopup(BasePopup):
    action_requested = Signal(str)  # moderation command text (placeholders expanded)

    def __init__(self, settings: Settings, target: UsercardTarget,
                 recent_entries: list[tuple[object, Message]], *,
                 self_is_mod: bool = False,
                 custom_actions: list[dict] | None = None, parent=None):
        super().__init__(parent)
        self.target = target
        self._avatar_url = ""
        self.setFixedSize(320, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # -- header: avatar + identity -----------------------------------------
        header = QHBoxLayout()
        self._avatar = QPushButton(self)
        self._avatar.setFixedSize(100, 100)
        self._avatar.setIconSize(QSize(100, 100))
        self._avatar.setCursor(Qt.CursorShape.PointingHandCursor)
        self._avatar.setStyleSheet("QPushButton { border: none; background: transparent; }")
        self._avatar.setToolTip("Open channel page")
        self._avatar.clicked.connect(self._open_profile)
        header.addWidget(self._avatar)

        identity = QVBoxLayout()
        self._name_label = QLabel(target.display_name or target.login, self)
        self._name_label.setStyleSheet(
            f"color: {target.color or '#e8e8e8'}; font-size: 13pt; font-weight: bold;")
        self._login_label = QLabel(f"@{target.login}", self)
        self._login_label.setStyleSheet("color: #8a8a8a;")
        identity.addWidget(self._name_label)
        identity.addWidget(self._login_label)
        identity.addStretch(1)
        header.addLayout(identity, 1)
        layout.addLayout(header)

        # -- detail rows (hidden until data exists) ------------------------------
        self._id_row, self._id_value = self._add_row("ID:")
        self._id_value.setText(target.user_id or "—")
        self._id_row.show()  # always visible; anonymous rows show "—"
        self._created_row, self._created_value = self._add_row("Created:")
        self._followers_row, self._followers_value = self._add_row(
            "Subscribers:" if target.platform is Platform.YOUTUBE else "Followers:")
        self._followage_row, self._followage_value = self._add_row("Follows you since:")

        # -- recent messages ------------------------------------------------------
        self._recent_label = QLabel("Recent messages", self)
        self._recent_label.setStyleSheet("color: #b0a8e8; font-weight: bold;")
        layout.addWidget(self._recent_label)
        self._recent = QListView(self)
        self._recent.setModel(ResultsModel(self._recent))
        self._recent.setItemDelegate(MessageDelegate(settings, self._recent))
        self._recent.setSelectionMode(QListView.SelectionMode.NoSelection)
        self._recent.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._recent.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._recent.setFixedHeight(140)
        layout.addWidget(self._recent)

        # -- moderation: buttons, not a menu ---------------------------------------
        self._mod_frame = QWidget(self)
        self._mod_layout = QHBoxLayout(self._mod_frame)
        self._mod_layout.setContentsMargins(0, 0, 0, 0)
        self._mod_layout.setSpacing(4)
        self._build_mod_buttons(custom_actions or [])
        layout.addWidget(self._mod_frame)
        self._set_mod_visible(self_is_mod and not target.is_self)

        layout.addStretch(1)

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

    def append_recent(self, entry: tuple[object, Message]) -> None:
        """Live append for messages from this user while the popup is open."""
        model = self._recent.model()
        model.insert_entry(model.rowCount(), entry)
        self._update_recent_label()
        self._recent.scrollToBottom()

    def show_at_cursor(self) -> None:
        """Open at the cursor (parity), clamped to the screen it sits on."""
        self.adjustSize()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        point = QCursor.pos() + QPoint(8, 8)
        point.setX(min(point.x(), geo.right() - self.width() + 1))
        point.setY(min(point.y(), geo.bottom() - self.height() + 1))
        self.move(point)
        self.show()
        self.raise_()

    # -- internals ------------------------------------------------------------------

    def _add_row(self, label_text: str) -> tuple[QWidget, QLabel]:
        row = QWidget(self)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        key = QLabel(label_text, row)
        key.setStyleSheet("color: #8a8a8a;")
        value = QLabel("", row)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(key)
        lay.addWidget(value, 1)
        row.hide()
        self.layout().addWidget(row)
        return row, value

    def _build_mod_buttons(self, custom_actions: list[dict]) -> None:
        twitch = self.target.platform is Platform.TWITCH
        self._mod_buttons: list[QPushButton] = []
        self._add_mod_button("Ban", f"/ban {self.target.login}")
        self._add_mod_button("Unban", f"/unban {self.target.login}")
        if twitch:
            for duration in _TIMEOUT_STRIP:
                self._add_mod_button(duration, f"/timeout {self.target.login} {duration}")
            for entry in custom_actions:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                template = str(entry.get("template") or "")
                text = (template.replace("{user.name}", self.target.display_name)
                               .replace("{user.login}", self.target.login))
                self._add_mod_button(str(entry["name"]), text)
        else:
            # YouTube: server ban + local hide for delete + fixed 300s timeout
            # (milestone decision — docs/m4.md).
            self._add_mod_button("Timeout 5m", f"/timeout {self.target.login} 300")
            self._add_mod_button("Delete", f"/delete {self.target.login}")
        self._mod_layout.addStretch(1)

    def _add_mod_button(self, label: str, text: str) -> None:
        button = QPushButton(label, self._mod_frame)
        button.clicked.connect(lambda checked=False, t=text: self.action_requested.emit(t))
        self._mod_layout.addWidget(button)
        self._mod_buttons.append(button)

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
        self._avatar.setIcon(QIcon(pixmap))
