"""Tab: one merged chat feed (Twitch channel + YouTube stream pair) with a
shared input bar whose messages post to both chats."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..config.settings import Settings
from ..models.message import Message
from .chat_view import ChatView
from .input_widget import InputWidget
from .tab_header import TabHeader


class Tab(QWidget):
    usercard_requested = Signal(object, str)  # Message, login
    reply_requested = Signal(object)          # Message
    search_requested = Signal(str, bool)      # selection, all_tabs
    reply_cancelled = Signal()
    offer_accept = Signal()  # user clicked "Send to X only"
    offer_edit = Signal()    # user clicked "Edit" on a blocked send
    mod_action_requested = Signal(object, str)  # Message, action name
    delete_requested = Signal(object)           # Message
    pin_requested = Signal(object)              # Message

    def __init__(self, settings: Settings, twitch_channel: str = "", youtube_title: str = "", parent=None):
        super().__init__(parent)
        self._settings = settings
        self.tab_key = uuid.uuid4().hex  # watch/send routing key
        self.twitch_channel = twitch_channel
        self.youtube_title = youtube_title
        self.youtube_target = ""        # video URL/id or @handle being watched
        self.youtube_live_chat_id = ""  # set once resolution succeeds
        self.youtube_video_id = ""
        self.youtube_channel_id = ""    # the stream's channel (moderation owner check)
        self.send_handler: Callable[[str], None] | None = None
        # Per-platform streamer-bar parts; the header status label shows
        # both joined with " · " (see set_*_status).
        self._twitch_status = ""
        self._youtube_status = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = TabHeader(settings, twitch_channel, youtube_title)
        self.view = ChatView(settings)
        self.input = InputWidget(settings)

        self._offer_banner = QFrame(self)
        self._offer_banner.setStyleSheet(
            "QFrame { background: #3a2a1e; border-top: 1px solid #5a4030; }"
            "QLabel { color: #e8c9a0; font-size: 9pt; }")
        offer_layout = QHBoxLayout(self._offer_banner)
        offer_layout.setContentsMargins(10, 4, 10, 4)
        self._offer_label = QLabel("", self._offer_banner)
        accept = QPushButton("", self._offer_banner)
        accept.clicked.connect(self.offer_accept)
        edit = QPushButton("Edit", self._offer_banner)
        edit.clicked.connect(self.offer_edit)
        offer_layout.addWidget(self._offer_label, 1)
        offer_layout.addWidget(accept)
        offer_layout.addWidget(edit)
        self._offer_button = accept
        self._offer_banner.hide()

        self._reply_banner = QFrame(self)
        self._reply_banner.setStyleSheet(
            "QFrame { background: #23233a; border-top: 1px solid #3a3a5a; }"
            "QLabel { color: #b0a8e8; font-size: 9pt; }")
        reply_layout = QHBoxLayout(self._reply_banner)
        reply_layout.setContentsMargins(10, 4, 10, 4)
        self._reply_label = QLabel("", self._reply_banner)
        cancel_reply = QPushButton("✕", self._reply_banner)
        cancel_reply.setToolTip("Cancel reply")
        cancel_reply.setStyleSheet(
            "QPushButton { background: transparent; color: #b0a8e8; border: none;"
            " font-size: 9pt; }")
        cancel_reply.clicked.connect(self._on_reply_cancelled)
        reply_layout.addWidget(self._reply_label, 1)
        reply_layout.addWidget(cancel_reply)
        self._reply_banner.hide()
        self._reply_parent: tuple[str, str] | None = None  # (msg id, lowercase login)
        self._reply_prefix = ""  # "@Name " currently in the input, if any

        self._pin_banner = QFrame(self)
        self._pin_banner.setStyleSheet(
            "QFrame { background: #3a3420; border-top: 1px solid #5a5030; }"
            "QLabel { color: #e0d090; font-size: 9pt; }")
        pin_layout = QHBoxLayout(self._pin_banner)
        pin_layout.setContentsMargins(10, 4, 10, 4)
        self._pin_label = QLabel("", self._pin_banner)
        unpin = QPushButton("✕", self._pin_banner)
        unpin.setToolTip("Unpin")
        unpin.setStyleSheet(
            "QPushButton { background: transparent; color: #e0d090; border: none;"
            " font-size: 9pt; }")
        unpin.clicked.connect(self.unpin)
        pin_layout.addWidget(self._pin_label, 1)
        pin_layout.addWidget(unpin)
        self._pin_banner.hide()

        layout.addWidget(self.header)
        layout.addWidget(self.view, 1)
        layout.addWidget(self._pin_banner)
        layout.addWidget(self._offer_banner)
        layout.addWidget(self._reply_banner)
        layout.addWidget(self.input)

        self.input.send_clicked.connect(self._on_send)
        self.view.usercard_requested.connect(self.usercard_requested)
        self.view.reply_requested.connect(self.reply_requested)
        self.view.search_requested.connect(self.search_requested)
        self.view.mod_action_requested.connect(self.mod_action_requested)
        self.view.delete_requested.connect(self.delete_requested)
        self.view.pin_requested.connect(self.pin_requested)

    # -- API ----------------------------------------------------------------------

    def set_send_handler(self, handler: Callable[[str], None]) -> None:
        self.send_handler = handler

    def add_message(self, msg: Message) -> None:
        self.view.add_message(msg)

    def set_twitch(self, channel: str, live: bool = False) -> None:
        self.twitch_channel = channel
        self.header.set_twitch(channel, live)

    def set_youtube(self, title: str, live: bool = False) -> None:
        self.youtube_title = title
        self.header.set_youtube(title, live)

    def show_pin(self, msg: Message) -> None:
        """Local pin banner ('Moderate' context menu)."""
        author = msg.author.name if msg.author is not None else "?"
        self._pin_label.setText(f"📌 {author}: {msg.plain}")
        self._pin_banner.show()

    def unpin(self) -> None:
        self._pin_banner.hide()

    def set_twitch_status(self, text: str) -> None:
        """Live-poll status ("Title · 1,234 viewers · up 1:02:03" / offline)."""
        self._twitch_status = text
        self._compose_status()

    def set_youtube_status(self, text: str) -> None:
        """Watch-state status (live stats, upcoming/offline/ended, quota…)."""
        self._youtube_status = text
        self._compose_status()

    def _compose_status(self) -> None:
        parts = [part for part in (self._twitch_status, self._youtube_status) if part]
        self.header.set_status(" · ".join(parts))

    def refresh_visuals(self) -> None:
        self.view.refresh_visuals()

    def show_offer(self, detail: str, accept_label: str) -> None:
        """Blocked-send banner: '… — [Send to Twitch only] [Edit]'."""
        self._offer_label.setText(detail)
        self._offer_button.setText(accept_label)
        self._offer_banner.show()

    def hide_offer(self) -> None:
        self._offer_banner.hide()

    def show_reply(self, parent_id: str, parent_login: str, display_name: str) -> None:
        """Twitch reply banner: stores the parent and puts '@Name ' into the
        input (replacing a previous mention if one is still there)."""
        prefix = f"@{display_name} "
        text = self.input.text()
        if self._reply_prefix and text.startswith(self._reply_prefix):
            self.input.set_text(prefix + text[len(self._reply_prefix):])
            self.input.set_cursor_position(len(prefix))
        else:
            self.input.insert_mention(display_name)
        self._reply_parent = (parent_id, parent_login)
        self._reply_prefix = prefix
        self._reply_label.setText(f"Replying to {prefix.strip()}")
        self._reply_banner.show()

    def hide_reply(self, *, remove_prefix: bool = False) -> None:
        """Clear reply state; cancel also strips the mention from the input."""
        if remove_prefix and self._reply_prefix:
            text = self.input.text()
            if text.startswith(self._reply_prefix):
                self.input.set_text(text[len(self._reply_prefix):])
        self._reply_parent = None
        self._reply_prefix = ""
        self._reply_banner.hide()

    def reply_prefix(self) -> str:
        """The '@Name ' prefix currently in the input ("" when not replying) —
        autocomplete ignores it for first-word detection."""
        return self._reply_prefix

    def consume_reply_prefix(self, text: str) -> tuple[str, str, str] | None:
        """If the banner is up and `text` still carries its '@Name ' prefix,
        strip it and return (rest, parent_id, parent_login) — the send goes
        out as a reply. The user editing the prefix away means a plain send."""
        if self._reply_parent is None or not self._reply_prefix:
            return None
        if not text.startswith(self._reply_prefix):
            return None
        rest = text[len(self._reply_prefix):]
        parent_id, parent_login = self._reply_parent
        self._reply_parent = None
        self._reply_prefix = ""
        self._reply_banner.hide()
        return rest, parent_id, parent_login

    def _on_reply_cancelled(self) -> None:
        self.hide_reply(remove_prefix=True)
        self.reply_cancelled.emit()

    def _on_send(self, text: str) -> None:
        if self.send_handler is not None:
            self.send_handler(text)
