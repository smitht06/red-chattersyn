"""MainWindow: menu bar + notebook of merged-feed tabs.

Non-demo mode owns TwitchService, YouTubeService and the SendCoordinator;
all messages/events/auth notices arrive through the Bridge and are routed
to the matching tab's feed. SendResult transitions drive the input chips,
system rows and the "Send to Twitch only" offer banner.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import json
import logging
import time
from collections.abc import Callable

from PySide6.QtCore import QSettings, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
)

from ..config.paths import config_dir
from ..config.secrets import SecretsStore
from ..config.settings import Settings
from ..core.bus import bridge
from ..core.text import TWITCH_MESSAGE_LIMIT, YOUTUBE_MESSAGE_LIMIT
from ..models.message import Author, Message, MessageFlags, Platform, TextRun
from ..services.autocomplete import build_candidates
from ..services.chat_index import ChatIndex
from ..services.commands import (
    CommandContext,
    CommandController,
    expand_placeholders,
    load_user_commands,
)
from ..services.emojis import emoji_dataset
from ..services.highlights import HighlightController
from ..services.logger import ChatLogger
from ..services.moderation import STACK_MAX_MESSAGES, STACK_WINDOW, ModerationState
from ..services.send import SendCoordinator, SendResult, SendTargets
from ..services.twitch.auth import DEFAULT_TWITCH_CLIENT_ID, TwitchAuthError
from ..services.twitch.mapper import ClearChat, ClearMsg, RoomState, UserState
from ..services.twitch.service import TwitchService
from ..services.youtube.auth import YouTubeAuthError
from ..services.youtube.resolver import MINE_TARGET
from ..services.youtube.service import WatchState, YouTubeService, YoutubeState
from . import theme
from .emote_picker import EmotePickerPopup
from .empty_tab_page import EmptyTabPage
from .fake_feed import FakeFeed
from .mentions_popup import MentionsPopup
from .notebook import Notebook
from .search_popup import SearchPopup
from .settings_dialog import SettingsDialog
from .tab import Tab
from .usercard import UsercardPopup, UsercardTarget, recent_messages_for

log = logging.getLogger(__name__)

_PLATFORM_LABEL = {Platform.TWITCH: "Twitch", Platform.YOUTUBE: "YouTube"}
IDENTITY_RETRY_SECONDS = 300.0  # pause between YouTube identity retry attempts
TWITCH_LIVE_POLL_SECONDS = 60.0  # Helix streams poll cadence per joined channel


def _chat_mode_text(state: dict) -> str:
    """Compact chat-mode summary from a mapped ROOMSTATE (docs/m4.md)."""
    parts = []
    if state.get("subs_only"):
        parts.append("subs-only")
    if state.get("emote_only"):
        parts.append("emote-only")
    if state.get("r9k"):
        parts.append("r9k")
    if state.get("followers_only", -1) >= 0:
        parts.append("followers-only")
    if state.get("slow"):
        parts.append(f"slow {state['slow']}s")
    return " · ".join(parts)


def _tab_title(twitch_channel: str, youtube_target: str) -> str:
    """Notebook label: only the chats the tab actually has, never a bare '?'."""
    if youtube_target == MINE_TARGET:
        # Stream tab: the Twitch side binds to the login when connected.
        return twitch_channel or "Your Stream"
    parts = []
    if twitch_channel:
        parts.append(twitch_channel)
    if youtube_target:
        target = youtube_target[:11] + "…" if len(youtube_target) > 11 else youtube_target
        parts.append(target)
    return " + ".join(parts)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, demo: bool = False,
                 qsettings: QSettings | None = None):
        self._qsettings = qsettings or QSettings()
        super().__init__()
        self._settings = settings
        self._demo = demo
        self._tabs: list[Tab] = []
        self._streamer_tabs: list[Tab] = []  # Stream tabs: own Twitch + own live YouTube
        self._feeds: list[FakeFeed] = []
        self._twitch: TwitchService | None = None
        self._youtube: YouTubeService | None = None
        self._send: SendCoordinator | None = None
        self._logger: ChatLogger | None = None
        self._commands: CommandController | None = None
        self._offers: dict[str, tuple[str, Platform]] = {}  # tab_key -> (key, platform)
        self._search_popup: SearchPopup | None = None
        self._highlights: HighlightController | None = None
        self._mentions_popup: MentionsPopup | None = None
        self._usercard_popup: UsercardPopup | None = None
        self._chat_index: ChatIndex | None = None
        self._emote_picker: EmotePickerPopup | None = None
        self._moderation = ModerationState()
        self._msgs_since_clearchat: dict[str, int] = {}  # twitch channel -> messages since last CC row
        self._clearchat_rows: dict[tuple[str, str, str], str] = {}  # (tab_key, channel, login) -> system row id
        self._identity_retry_at = 0.0  # monotonic stamp of the last identity retry
        self._identity_retry_task: asyncio.Task | None = None
        self._roomstate_text: dict[str, str] = {}  # twitch channel -> last chat-mode label
        self._twitch_live: dict[str, dt.datetime] = {}  # channel -> last poll saw it live
        self._twitch_offline_since: dict[str, dt.datetime] = {}  # channel -> went down at

        self.setWindowTitle("ChatterSyn")
        self.resize(1080, 720)

        self._secrets = SecretsStore(config_dir())
        if not demo:
            self._twitch = TwitchService(settings, self._secrets)
            self._youtube = YouTubeService(settings, self._secrets)
            self._send = SendCoordinator(
                self._twitch.send, self._youtube.send, settings,
                twitch_author=self._twitch.self_author,
                youtube_author=self._youtube.self_author,
                emote_registry=self._twitch.registry,
            )
            self._send.start()
            self._logger = ChatLogger(settings)
            self._logger.start()
            # Before the bridge->tab router connects below: the controller sets
            # HIGHLIGHTED so rows first paint in the highlighted state.
            self._highlights = HighlightController(settings)
            self._highlights.set_identity_provider(self._twitch.self_author)
            self._highlights.start(self)
            self._highlights.highlighted.connect(self._on_highlighted)
            self._commands = CommandController(settings, load_user_commands(config_dir()))
            self._chat_index = ChatIndex()
            self._chat_index.start()

        self._notebook = Notebook(self)
        self._notebook.new_tab_requested.connect(self._new_streamer_tab)
        self._notebook.tabCloseRequested.connect(self._close_tab)
        # Stack: notebook while tabs exist, link page when the last one closes.
        self._empty_tab_page = EmptyTabPage(self)
        self._empty_tab_page.new_stream.connect(self._new_streamer_tab)
        self._empty_tab_page.new_twitch.connect(self._new_twitch_tab)
        self._empty_tab_page.new_youtube.connect(self._new_youtube_tab)
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._notebook)
        self._stack.addWidget(self._empty_tab_page)
        self.setCentralWidget(self._stack)
        self._sync_empty_state()

        self._build_menus()
        self._setup_hotkeys()
        self._restore_geometry()
        self.statusBar().showMessage(
            "Demo feed (M1) — M2/M3 wire Twitch IRC + YouTube live chat"
            if demo
            else "Connecting to Twitch…"
        )

        if not demo:
            bridge.message.connect(self._on_bridge_message)
            bridge.message.connect(self._on_usercard_live)
            bridge.event.connect(self._on_bridge_event)
            bridge.auth_state.connect(self._on_auth_state)
            bridge.send_result.connect(self._on_send_result)
            asyncio.ensure_future(self._start_services())

        saved_tabs = self._settings.get("tabs") or []
        if saved_tabs and self._settings.get("general.restore_tabs", True):
            for tab_conf in saved_tabs:
                if tab_conf.get("type") == "streamer":
                    # A saved Stream tab: the stored twitch_channel is stale
                    # (or empty) — binding re-derives it from live tokens.
                    self._new_streamer_tab()
                    continue
                youtube_conf = tab_conf.get("youtube") or {}
                self.add_tab(
                    tab_conf.get("twitch_channel", ""),
                    youtube_conf.get("video_id") or youtube_conf.get("channel_id") or "",
                )
        else:
            self.add_tab("forsen", None)

        theme.theme_notifier.changed.connect(self._on_theme_changed)

    # -- menus -----------------------------------------------------------------------

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        # Shortcuts for New Tab / Close Tab live in the hotkey registry
        # (settings hotkeys.*) — the menu actions carry no key of their own.
        stream_tab_action = QAction("New Stream Tab", self)
        stream_tab_action.triggered.connect(self._new_streamer_tab)
        file_menu.addAction(stream_tab_action)

        twitch_tab_action = QAction("New Twitch Tab", self)
        twitch_tab_action.triggered.connect(self._new_twitch_tab)
        file_menu.addAction(twitch_tab_action)

        youtube_tab_action = QAction("New YouTube Tab", self)
        youtube_tab_action.triggered.connect(self._new_youtube_tab)
        file_menu.addAction(youtube_tab_action)

        file_menu.addSeparator()

        close_tab_action = QAction("Close Tab", self)
        close_tab_action.triggered.connect(lambda: self._close_tab(self._notebook.currentIndex()))
        file_menu.addAction(close_tab_action)

        file_menu.addSeparator()
        connect_youtube_action = QAction("Connect YouTube Account…", self)
        connect_youtube_action.triggered.connect(self._connect_youtube)
        file_menu.addAction(connect_youtube_action)

        connect_twitch_action = QAction("Connect Twitch Account…", self)
        connect_twitch_action.triggered.connect(self._connect_twitch)
        file_menu.addAction(connect_twitch_action)

        file_menu.addSeparator()
        preferences_action = QAction("Preferences…", self)
        preferences_action.triggered.connect(self._open_settings)
        file_menu.addAction(preferences_action)

        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(QApplication.instance().quit)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("&View")
        theme_action = QAction("Switch Light/Dark Theme", self)
        theme_action.triggered.connect(self._toggle_theme)
        view_menu.addAction(theme_action)
        mentions_action = QAction("Mentions", self)
        mentions_action.triggered.connect(self._open_mentions)
        view_menu.addAction(mentions_action)
        picker_action = QAction("Emote Picker", self)
        picker_action.triggered.connect(self._open_emote_picker_for_current)
        view_menu.addAction(picker_action)
        view_menu.addSeparator()
        zoom_in = QAction("Zoom In", self)
        zoom_in.setShortcut(QKeySequence.ZoomIn)
        zoom_in.triggered.connect(lambda: self._zoom(1.1))
        view_menu.addAction(zoom_in)
        zoom_out = QAction("Zoom Out", self)
        zoom_out.setShortcut(QKeySequence.ZoomOut)
        zoom_out.triggered.connect(lambda: self._zoom(1 / 1.1))
        view_menu.addAction(zoom_out)
        zoom_reset = QAction("Reset Zoom", self)
        zoom_reset.setShortcut(QKeySequence("Ctrl+0"))
        zoom_reset.triggered.connect(lambda: self._zoom(1.0, reset=True))
        view_menu.addAction(zoom_reset)

        help_menu = self.menuBar().addMenu("&Help")
        about_action = QAction("About ChatterSyn", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # -- settings + hotkeys --------------------------------------------------------

    def _setup_hotkeys(self) -> None:
        """Central hotkey registry: one QShortcut per settings action.
        WindowShortcut context (the default) means the shortcut only fires
        when the focused widget doesn't consume the key — the chat view's
        own Ctrl+F handler wins while the chat has focus."""
        self._hotkey_shortcuts: dict[str, QShortcut] = {}
        self._rebind_hotkeys()

    def _rebind_hotkeys(self) -> None:
        for shortcut in self._hotkey_shortcuts.values():
            shortcut.deleteLater()
        self._hotkey_shortcuts.clear()
        for name, handler in self._hotkey_handlers().items():
            sequence = self._settings.get(f"hotkeys.{name}") or ""
            if not sequence:
                continue
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)
            self._hotkey_shortcuts[name] = shortcut

    def _hotkey_handlers(self) -> dict[str, Callable[[], None]]:
        return {
            "open_settings": self._open_settings,
            "new_tab": self._new_streamer_tab,
            "close_tab": lambda: self._close_tab(self._notebook.currentIndex()),
            "next_tab": self._notebook_next,
            "prev_tab": self._notebook_prev,
            "focus_input": self._focus_input,
            "toggle_search": self._toggle_search,
            "toggle_theme": self._toggle_theme,
            "emote_picker": self._open_emote_picker_for_current,
        }

    def _notebook_next(self) -> None:
        count = self._notebook.count()
        if count:
            self._notebook.setCurrentIndex((self._notebook.currentIndex() + 1) % count)

    def _notebook_prev(self) -> None:
        count = self._notebook.count()
        if count:
            self._notebook.setCurrentIndex((self._notebook.currentIndex() - 1) % count)

    def _focus_input(self) -> None:
        widget = self._notebook.currentWidget()
        if isinstance(widget, Tab):
            widget.input.focus_input()

    def _toggle_search(self) -> None:
        widget = self._notebook.currentWidget()
        if isinstance(widget, Tab):
            self._on_search_requested(widget, "", False)

    def _open_settings(self) -> None:
        twitch_status, youtube_status = "Not connected", "Not connected"
        if not self._demo:
            if self._twitch is not None and self._twitch.tokens is not None:
                login = self._twitch.tokens.login or ""
                twitch_status = f"Connected as {login}" if login else "Connected"
            if self._youtube is not None and self._youtube.tokens is not None:
                youtube_status = "Connected"
            elif self._youtube is not None and self._youtube.auth.configured():
                youtube_status = "Credentials configured — not connected"
        dialog = SettingsDialog(
            self._settings, config_dir(),
            twitch_status=twitch_status,
            youtube_status=youtube_status,
            youtube_api_key="" if self._demo else (self._secrets.get("youtube_api_key") or ""),
            on_youtube_api_key=None if self._demo else self._save_youtube_api_key,
            connect_twitch=None if self._demo else self._connect_twitch,
            disconnect_twitch=None if self._demo else self._disconnect_twitch_account,
            connect_youtube=None if self._demo else self._connect_youtube,
            disconnect_youtube=None if self._demo else self._disconnect_youtube_account,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_settings()

    def _save_youtube_api_key(self, value: str) -> None:
        """The Data API key lives in the keyring — an API key in settings.json
        is too leakable."""
        if value:
            self._secrets.set("youtube_api_key", value)
        else:
            self._secrets.delete("youtube_api_key")

    def _apply_settings(self) -> None:
        """Push the just-accepted settings snapshot into the live UI."""
        theme.apply(QApplication.instance(), self._settings.get("theme", "dark"),
                    self._settings.get("ui_scale", 1.0))
        scrollback = int(self._settings.get("scrollback", 1000) or 1000)
        for tab in self._tabs:
            # Existing models trim to the new cap on their next flush; new
            # tabs read the setting at construction.
            tab.view.message_model.max_messages = scrollback
            tab.view.set_moderation_actions(self._mod_action_names())
        self._rebind_hotkeys()
        if self._demo:
            return
        if self._twitch is not None:
            # The client_id was snapshotted when the service was built —
            # refresh it so Connect works without a restart, and rebuild the
            # Helix client if the settings override changed.
            self._twitch.auth.client_id = (
                self._settings.get("twitch.client_id") or DEFAULT_TWITCH_CLIENT_ID
            )
            asyncio.ensure_future(self._twitch.rebuild_helix())
        if self._youtube is not None:
            # The API key was snapshotted at service construction — refresh it
            # from the keyring so a changed key applies without a restart.
            self._youtube.set_api_key(self._secrets.get("youtube_api_key") or "")
        if self._commands is not None:
            self._commands.set_user_commands(load_user_commands(config_dir()))

    def _disconnect_twitch_account(self) -> None:
        if self._twitch is not None:
            asyncio.ensure_future(self._disconnect_twitch())

    async def _disconnect_twitch(self) -> None:
        assert self._twitch is not None
        await self._twitch.disconnect()
        bridge.auth_state.emit("twitch", False, "signed out — read-only mode")

    def _disconnect_youtube_account(self) -> None:
        if self._youtube is None:
            return
        self._youtube.auth.clear_token()
        # The service and MainWindow auth_state handlers drop the identity
        # and moderation ownership in response to the failure emit.
        bridge.auth_state.emit("youtube", False, "signed out")

    # -- geometry --------------------------------------------------------------------

    def _restore_geometry(self) -> None:
        geometry = self._qsettings.value("main_window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = self._qsettings.value("main_window/state")
        if state is not None:
            self.restoreState(state)

    def _save_geometry(self) -> None:
        self._qsettings.setValue("main_window/geometry", self.saveGeometry())
        self._qsettings.setValue("main_window/state", self.saveState())

    # -- services ----------------------------------------------------------------

    async def _start_services(self) -> None:
        assert self._twitch is not None
        try:
            await self._twitch.start()
            self.statusBar().showMessage(
                "Twitch connected (read-only — connect an account to send)", 8000)
        except Exception as exc:
            log.error("Twitch service start failed: %s", exc)
            self.statusBar().showMessage(f"Twitch: {exc}")
            for tab in self._tabs:
                tab.add_message(
                    Message.system(Platform.TWITCH, tab.twitch_channel,
                                   f"Twitch unavailable: {exc}"))
        # Stream tabs restored before the services started: bind their Twitch
        # side now that validated tokens (if any) are in place.
        self._bind_streamer_tabs()
        if self._youtube is not None:
            try:
                await self._youtube.start()
                # start() awaits the identity load, so the ownership check
                # can run deterministically now — a LIVE event may already
                # have arrived before the identity was known.
                self._refresh_youtube_ownership()
            except Exception as exc:
                log.error("YouTube service start failed: %s", exc)
        # Live-status poller: the loop body skips ticks while the Helix
        # client is missing (no app credentials yet), so a late create via
        # the settings dialog is picked up automatically.
        asyncio.ensure_future(self._poll_twitch_live())

    async def _poll_twitch_live(self) -> None:
        """Helix streams poll per joined channel (~60 s): drives the header
        live dot and the uptime/viewers status (or "offline since HH:MM"
        after a stream goes down)."""
        while True:
            await asyncio.sleep(TWITCH_LIVE_POLL_SECONDS)
            if self._twitch is None or self._twitch.helix is None:
                continue
            channels = {tab.twitch_channel for tab in self._tabs if tab.twitch_channel}
            for channel in channels:
                try:
                    stream = await self._twitch.helix.streams(channel)
                except Exception as exc:  # network/HTTP: skip, retry next tick
                    log.warning("Twitch live poll failed for %s: %s", channel, exc)
                    continue
                self._apply_twitch_stream(channel, stream)

    def _apply_twitch_stream(self, channel: str, stream: dict | None) -> None:
        now = dt.datetime.now()
        if stream is not None:
            self._twitch_live[channel] = now
            self._twitch_offline_since.pop(channel, None)
            text = self._format_stream_status(
                stream.get("title") or "", stream.get("viewer_count") or 0,
                stream.get("started_at") or "")
        else:
            offline = self._twitch_offline_since.get(channel)
            if channel in self._twitch_live and offline is None:
                offline = now  # first poll after it went down: record when
                self._twitch_offline_since[channel] = offline
            self._twitch_live.pop(channel, None)
            text = f"offline since {offline:%H:%M}" if offline else ""
        for tab in self._tabs:
            if tab.twitch_channel == channel:
                tab.header.set_twitch(channel, live=stream is not None)
                tab.set_twitch_status(text)

    @staticmethod
    def _format_stream_status(title: str, viewers: int, started_at: str) -> str:
        """'Title · 1,234 viewers · up 1:02:03' (parts missing -> dropped)."""
        parts: list[str] = []
        if title:
            parts.append(title)
        if viewers:
            parts.append(f"{viewers:,} viewers")
        if started_at:
            try:
                started = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                delta = dt.datetime.now(dt.UTC) - started
                total = int(delta.total_seconds())
                if total >= 0:
                    hours, rem = divmod(total, 3600)
                    minutes, seconds = divmod(rem, 60)
                    parts.append(f"up {hours}:{minutes:02d}:{seconds:02d}")
            except ValueError:
                pass  # malformed started_at: skip the uptime part
        return " · ".join(parts) or "live"

    async def shutdown(self) -> None:
        if self._send is not None:
            await self._send.stop()
        if self._youtube is not None:
            await self._youtube.stop()
        if self._twitch is not None:
            await self._twitch.stop()
        if self._logger is not None:
            await self._logger.stop()  # last: drain tail lines after services stop
        if self._highlights is not None:
            self._highlights.stop()
        if self._chat_index is not None:
            self._chat_index.stop()

    # -- bridge routing -----------------------------------------------------------------

    def _on_bridge_message(self, msg: Message) -> None:
        if msg.platform is Platform.YOUTUBE:
            if self._send is not None and self._send.note_echo(msg):
                return  # our own send echoing back — optimistic row already shown
            for tab in self._tabs:
                if msg.channel_id and tab.youtube_live_chat_id == msg.channel_id:
                    tab.add_message(msg)
                    continue  # several tabs may share one live chat
                if msg.extra.get("watch_key") == tab.tab_key:
                    tab.add_message(msg)
            return
        routed = False
        for tab in self._tabs:
            if tab.twitch_channel == msg.channel_id:
                tab.add_message(msg)
                routed = True
        if routed:
            # "(N times)" stacking window: count rows since the last
            # ClearChat row; a new post by the user restarts their stack.
            # Counted once per inbound message — several tabs may share one
            # channel, and counting per tab would close the window early.
            self._msgs_since_clearchat[msg.channel_id] = (
                self._msgs_since_clearchat.get(msg.channel_id, 0) + 1)
            if msg.author is not None:
                self._moderation.clear_stack(msg.channel_id, msg.author.login)

    def _on_bridge_event(self, event) -> None:
        if isinstance(event, ClearChat):
            if event.user is None:
                # Full clear: rows stay visible but grayed (docs/m4.md
                # "disable all") — scrollback survives for search/copy.
                # The clear starts a new epoch: break every "(N times)"
                # stack and the 20-message window so a later timeout folds
                # below the clear marker, not onto the pre-clear row.
                self._msgs_since_clearchat[event.channel] = 0
                self._moderation.clear_stacks_for_channel(event.channel)
                for tab in self._tabs:
                    if tab.twitch_channel != event.channel:
                        continue
                    tab.view.message_model.update_flags_for_channel(
                        event.channel, MessageFlags.DISABLED, True)
                    tab.add_message(Message.system(
                        Platform.TWITCH, event.channel,
                        "Chat has been cleared by a moderator."))
                return
            # Bookkeeping runs ONCE per event — several tabs may share the
            # channel, and per-tab runs would inflate stack counts and the
            # 20-message window.
            since = self._msgs_since_clearchat.get(event.channel, 0)
            self._msgs_since_clearchat[event.channel] = 0
            login = event.user.lower()
            if event.duration == 0:
                # /untimeout arrives as a ban-duration=0 ClearChat: un-gray,
                # no stack entry, no "(N times)" inflation.
                self._moderation.clear_disabled(event.channel, login)
                self._moderation.clear_stack(event.channel, login)
                for tab in self._tabs:
                    if tab.twitch_channel != event.channel:
                        continue
                    tab.view.message_model.update_flags_for_user(
                        event.channel, login, MessageFlags.DISABLED, False)
                    tab.add_message(Message.system(
                        Platform.TWITCH, event.channel, f"{event.user} was unbanned."))
                return
            if not self._clearchat_stackable(event.channel, login, since):
                self._moderation.clear_stack(event.channel, login)
            count = self._moderation.note_clearchat(
                event.channel, login, event.duration)
            for tab in self._tabs:
                if tab.twitch_channel != event.channel:
                    continue
                # The user's rows stay visible but grayed (disabled overlay).
                tab.view.message_model.update_flags_for_user(
                    event.channel, login, MessageFlags.DISABLED, True)
                if count > 1:
                    row_id = self._clearchat_rows.get(
                        (tab.tab_key, event.channel, login))
                    if row_id is not None and tab.view.message_model.update_message(
                            row_id, functools.partial(
                                self._restack_clearchat, event=event, count=count)):
                        continue  # stacked "(N times)" onto the existing row
                msg = self._clearchat_row(tab, event, count)
                tab.add_message(msg)
                self._clearchat_rows[(tab.tab_key, event.channel, login)] = msg.id
            if event.duration is not None and event.duration > 0:
                # The timeout expires on its own: un-gray then. The timer is
                # armed with the deadline it corresponds to — coarse timers
                # fire up to ~5% early, and comparing against the wall clock
                # at fire time would misread an early fire as "a newer ban
                # is active" and never un-gray.
                deadline = self._moderation.disabled_until(event.channel, login)
                QTimer.singleShot(
                    event.duration * 1000,
                    functools.partial(self._expire_disable, event.channel, login, deadline))
        elif isinstance(event, ClearMsg):
            for tab in self._tabs:
                if tab.twitch_channel == event.channel:
                    tab.view.message_model.mark_deleted(event.target_msg_id)
        elif isinstance(event, UserState):
            self._on_user_state(event)
        elif isinstance(event, RoomState):
            # Chat-mode indicator (docs/m4.md: ROOMSTATE-driven menu; YouTube
            # exposes no in-feed chat-mode signal, so Twitch-only). The last
            # label is cached so tabs added later (same channel, no fresh
            # ROOMSTATE — the IRC join is a no-op) replay it immediately.
            text = _chat_mode_text(event.state)
            self._roomstate_text[event.channel] = text
            for tab in self._tabs:
                if tab.twitch_channel == event.channel:
                    tab.header.set_chat_modes(text)
        elif isinstance(event, YoutubeState):
            self._on_youtube_state(event)

    def _expire_disable(self, channel: str, login: str, deadline: float | None) -> None:
        """A timeout's duration has passed: un-gray the user's rows.
        `deadline` is the entry value this timer was armed for — if a newer
        timeout/ban replaced it (or an untimeout cleared it), leave the rows
        alone. A missing entry means it was pruned as expired by an
        is_disabled/disabled_logins call: nothing newer is active, so the
        rows must still un-gray."""
        if self._moderation.disabled_until(channel, login) != deadline:
            if self._moderation.is_disabled(channel, login):
                return
        else:
            self._moderation.clear_disabled(channel, login)
        for tab in self._tabs:
            if tab.twitch_channel == channel:
                tab.view.message_model.update_flags_for_user(
                    channel, login, MessageFlags.DISABLED, False)

    def _clearchat_row(self, tab: Tab, event: ClearChat, count: int) -> Message:
        """The "(N times)"-stackable system row for a timeout/ban."""
        what = ("permanently banned" if event.duration is None
                else f"timed out for {event.duration}s")
        times = f" ({count} times)" if count > 1 else ""
        return Message.system(
            Platform.TWITCH, event.channel,
            f"{event.user} was {what}.{times}",
            extra={"clearchat_user": event.user.lower()})

    @staticmethod
    def _restack_clearchat(m: Message, event: ClearChat, count: int) -> None:
        what = ("permanently banned" if event.duration is None
                else f"timed out for {event.duration}s")
        m.plain = f"{event.user} was {what}. ({count} times)"
        m.runs = [TextRun(m.plain)]
        m.extra["clearchat_user"] = event.user.lower()

    def _clearchat_stackable(self, channel: str, login: str, since: int) -> bool:
        """True when a new ClearChat folds onto the previous row (5s /
        20-message window; docs/m4.md). `since` is the message count before
        this ClearChat."""
        last = self._moderation.last_clearchat(channel, login)
        if last is None:
            return False
        if time.monotonic() - last > STACK_WINDOW:
            return False
        return since <= STACK_MAX_MESSAGES

    def _on_user_state(self, event: UserState) -> None:
        """USERSTATE: per-channel mod rights for the connected account."""
        channel = event.channel.lstrip("#").lower()
        self._moderation.set_twitch_mod(channel, event.mod)
        self._moderation.note_twitch_self_badges(channel, event.badges)
        author = self._twitch.self_author() if self._twitch is not None else None
        if author is not None:
            self._moderation.set_self_identity(author.name)
        for tab in self._tabs:
            if tab.twitch_channel == channel:
                self._update_moderation_available(tab)

    def _on_youtube_state(self, event: YoutubeState) -> None:
        tab = self._tab_by_key(event.watch_key)
        if tab is None:
            return
        if event.live_chat_id:
            tab.youtube_live_chat_id = event.live_chat_id
        if event.video_id:
            tab.youtube_video_id = event.video_id
        if event.channel_id:
            tab.youtube_channel_id = event.channel_id
        state = event.state
        if state is WatchState.LIVE:
            if self._youtube is not None and event.live_chat_id and event.channel_id:
                if self._youtube.self_channel_id():
                    # Ownership known: record it (shield/usercard gating).
                    self._moderation.set_youtube_owner(
                        event.live_chat_id,
                        self._youtube.owns_channel(event.channel_id))
                elif self._youtube.tokens is not None:
                    # Identity unknown (its startup fetch failed): retry it.
                    # The event gate fires at most once per 5 minutes and
                    # never while a retry loop is running; the loop itself
                    # keeps retrying until the identity resolves — a stable
                    # reader connection emits no further LIVE events to
                    # re-trigger on.
                    now = time.monotonic()
                    if (now - self._identity_retry_at > 300
                            and (self._identity_retry_task is None
                                 or self._identity_retry_task.done())):
                        self._identity_retry_at = now
                        self._identity_retry_task = asyncio.ensure_future(
                            self._retry_identity())
            tab.set_youtube(event.title or tab.youtube_title, live=True)
            # Stats come from the initial resolution and the ~120 s refresh
            # (service _stats_loop); parts present at the time are shown.
            tab.set_youtube_status(self._format_stream_status(
                event.channel_title or "", event.viewers or 0,
                event.started_at.isoformat() if event.started_at else "") or "live")
            tab.header.set_youtube_send_enabled(True)
        elif state is WatchState.UPCOMING:
            tab.set_youtube(event.title or tab.youtube_title, live=False)
            tab.set_youtube_status("upcoming — connecting when the stream starts")
        elif state is WatchState.OFFLINE:
            tab.set_youtube(event.title or tab.youtube_title, live=False)
            # offlineAt: the header keeps "offline since HH:MM" until it goes
            # live again (the resolver re-checks periodically).
            tab.set_youtube_status(
                "offline — connecting when the stream starts"
                f" · offline since {dt.datetime.now():%H:%M}")
        elif state is WatchState.ENDED:
            tab.set_youtube(event.title or tab.youtube_title, live=False)
            tab.set_youtube_status(f"stream ended · offline since {dt.datetime.now():%H:%M}")
        elif state is WatchState.UNAVAILABLE:
            tab.set_youtube_status("chat unavailable")
            if event.detail:
                tab.add_message(Message.system(
                    Platform.YOUTUBE, event.live_chat_id or "",
                    f"YouTube chat unavailable: {event.detail}",
                    extra={"watch_key": tab.tab_key}))
        elif state is WatchState.QUOTA_EXHAUSTED:
            tab.set_youtube_status("YouTube quota exhausted")
            # Sends bill 20 units per insert and would be rejected anyway —
            # block the toggle until the quota resets (midnight PT) and the
            # watch goes LIVE again.
            tab.header.set_youtube_send_enabled(
                False, "YouTube quota exhausted — resets midnight PT")
        elif state is WatchState.AUTH_REQUIRED:
            tab.set_youtube_status("YouTube auth required")
            tab.header.set_youtube_send_enabled(
                False, "Connect a Google account (Settings → Accounts) to send")
        elif state is WatchState.NOT_FOUND:
            tab.set_youtube("—", live=False)
            tab.set_youtube_status("not found — check the URL/handle")
        elif state is WatchState.RESOLVING:
            tab.set_youtube_status("resolving…")
        if (state in (WatchState.UPCOMING, WatchState.OFFLINE, WatchState.ENDED,
                      WatchState.UNAVAILABLE, WatchState.NOT_FOUND)
                and tab.youtube_live_chat_id):
            # Every non-live resolution state means the old live chat is
            # gone: drop the dead id, its ownership entry and the ban-id
            # memory so Ban/Unban/Hide stop targeting it (and the shield
            # switches off during the re-resolve gap). NOT_FOUND/OFFLINE/
            # UPCOMING are normally unreachable with a live id (ENDED/
            # UNAVAILABLE always precede them) but guard the exception
            # bypass in the watch loop. QUOTA_EXHAUSTED/AUTH_REQUIRED keep
            # the id — the chat is still alive, just unwatchable.
            self._moderation.forget_youtube_chat(tab.youtube_live_chat_id)
            tab.youtube_live_chat_id = ""
        self._update_moderation_available(tab)

    # -- moderation mode ---------------------------------------------------------------

    def _update_moderation_available(self, tab: Tab) -> None:
        """Shield available when the connected account has rights on either
        platform of the pair (docs/m4.md: YT ownership unknown -> hidden)."""
        twitch = bool(tab.twitch_channel) and self._moderation.has_twitch_rights(
            tab.twitch_channel)
        youtube = bool(tab.youtube_live_chat_id) and self._moderation.has_youtube_rights(
            tab.youtube_live_chat_id)
        tab.header.set_moderation_available(twitch or youtube)

    def _refresh_youtube_ownership(self) -> None:
        """Re-run the owner check once the connected identity is known
        (identity loads after a LIVE event when connecting mid-stream)."""
        if self._youtube is None or not self._youtube.self_channel_id():
            return
        for tab in self._tabs:
            if tab.youtube_live_chat_id and tab.youtube_channel_id:
                self._moderation.set_youtube_owner(
                    tab.youtube_live_chat_id,
                    self._youtube.owns_channel(tab.youtube_channel_id))
                self._update_moderation_available(tab)

    async def _retry_identity(self) -> None:
        """Identity fetch failed at startup: retry until it resolves (or the
        account signs out), re-checking ownership for every live tab after
        each attempt. LIVE events won't re-trigger on a stable reader
        connection, so the loop owns its cadence."""
        if self._youtube is None:
            return
        while self._youtube.tokens is not None:
            await self._youtube.load_identity()
            self._refresh_youtube_ownership()
            if self._youtube.self_channel_id():
                return
            await asyncio.sleep(IDENTITY_RETRY_SECONDS)

    def _on_moderation_toggled(self, tab: Tab, enabled: bool) -> None:
        tab.view.set_moderation_enabled(enabled)

    def _mod_action_names(self) -> list[str]:
        """Configured moderation-action labels (settings moderation_actions)."""
        names: list[str] = []
        for entry in self._settings.get("moderation_actions", []) or []:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(str(entry["name"]))
        return names

    def _mod_template(self, action: str) -> str | None:
        for entry in self._settings.get("moderation_actions", []) or []:
            if isinstance(entry, dict) and entry.get("name") == action:
                return str(entry.get("template") or "")
        return None

    def _on_mod_action(self, tab: Tab, msg: Message, action: str) -> None:
        """Per-row button: Twitch expands the configured template and runs it
        through the command controller; YouTube uses its fixed action set."""
        if msg.platform is Platform.YOUTUBE:
            self._on_youtube_mod_action(tab, msg, action)
            return
        template = self._mod_template(action)
        if template is None or msg.author is None:
            return
        try:
            text = expand_placeholders(template, [], {
                "user.name": msg.author.name,
                "user.login": msg.author.login,
                "msg.id": msg.id,
                "msg.text": msg.plain,
                "channel.name": tab.twitch_channel,
            })
        except Exception:
            return
        if text.strip():
            self._run_command(tab, text)

    def _on_youtube_mod_action(self, tab: Tab, msg: Message, action: str) -> None:
        """Per-row YouTube buttons: server Ban / fixed 300s Timeout / local Hide."""
        if msg.author is None or msg.is_system:
            return
        channel_id = msg.author.user_id
        if not channel_id:
            self._yt_note(tab, f"no channel id recorded for {msg.author.name} — action skipped")
            return
        if action == "Ban":
            asyncio.ensure_future(self._yt_ban(tab, channel_id))
        elif action == "Timeout":
            asyncio.ensure_future(self._yt_ban(tab, channel_id, duration=300))
        elif action == "Hide":
            # The chat the row came from, not the tab's current id: rows
            # survive a UNAVAILABLE/ENDED that clears the tab id, and hiding
            # them locally must keep working.
            self._yt_hide_user(tab, msg.channel_id or tab.youtube_live_chat_id,
                               msg.author.login, msg.author.name)

    async def _yt_ban(self, tab: Tab, channel_id: str, duration: int = 0) -> None:
        """liveChatBans.insert (permanent, or `duration` seconds). The ban id
        comes back in the insert response and is remembered for Unban; the
        live chat itself emits the ban event, so only failures add a row."""
        assert self._youtube is not None
        lcid = tab.youtube_live_chat_id
        if not lcid:
            self._yt_note(tab, "no live chat connected — ban not sent")
            return
        try:
            resp = await self._youtube.api.live_chat_ban(lcid, channel_id, duration=duration)
        except Exception as exc:
            self._yt_note(tab, f"YouTube ban failed: {exc}")
            return
        ban_id = resp.get("id") if isinstance(resp, dict) else None
        if ban_id:
            self._moderation.remember_youtube_ban(lcid, channel_id, ban_id)

    async def _yt_unban(self, tab: Tab, channel_id: str, name: str) -> None:
        """liveChatBans.delete with the remembered ban id."""
        assert self._youtube is not None
        lcid = tab.youtube_live_chat_id
        if not lcid:
            self._yt_note(tab, "no live chat connected — unban not sent")
            return
        ban_id = self._moderation.youtube_ban_id(lcid, channel_id)
        if ban_id is None:
            self._yt_note(tab, f"no ban recorded for {name}")
            return
        try:
            await self._youtube.api.live_chat_unban(ban_id)
        except Exception as exc:
            self._yt_note(tab, f"YouTube unban failed: {exc}")
            return
        self._moderation.clear_youtube_ban(lcid, channel_id)
        self._yt_note(tab, f"unbanned {name}")

    def _yt_hide_user(self, tab: Tab, channel_id: str, login: str, name: str) -> None:
        """Local hide ('delete'): gray the user's rows without a server call."""
        rows = tab.view.message_model.update_flags_for_user(
            channel_id, login, MessageFlags.DISABLED, True)
        self._yt_note(tab, f"You hid {name}'s messages ({rows} row{'s' if rows != 1 else ''})")

    def _yt_note(self, tab: Tab, text: str) -> None:
        tab.add_message(Message.system(
            Platform.YOUTUBE, tab.youtube_live_chat_id or "",
            text, extra={"watch_key": tab.tab_key}))

    def _on_delete_requested(self, tab: Tab, msg: Message) -> None:
        """'Moderate' > Delete message: YouTube = local hide of the one row;
        Twitch's Helix delete needs channel:moderate and is deferred
        (docs/m4.md)."""
        if msg.platform is Platform.YOUTUBE:
            tab.view.message_model.update_message(
                msg.id, lambda m: setattr(m, "flags", m.flags | MessageFlags.DISABLED))
            return
        tab.add_message(Message.system(
            Platform.TWITCH, tab.twitch_channel,
            "Delete message needs channel:moderate — not implemented yet"))

    def _on_pin_requested(self, tab: Tab, msg: Message) -> None:
        tab.show_pin(msg)

    def _on_auth_state(self, platform: str, ok: bool, detail: str) -> None:
        if ok:
            self.statusBar().showMessage(f"{platform} authenticated", 5000)
            if platform == "twitch":
                self._bind_streamer_tabs()
            elif platform == "youtube":
                for tab in self._tabs:
                    tab.header.set_youtube_send_enabled(True)
                # Fresh tokens apply immediately instead of after the
                # 60-120 s poll sleep of the existing watch loop.
                self._rewatch_streamer_tabs()
            return
        self.statusBar().showMessage(f"{platform}: {detail}")
        if platform == "youtube":
            # The service cleared the identity — drop every recorded
            # ownership entry or the shield would keep stale mine=True rows.
            self._moderation.clear_youtube_ownership()
            for tab in self._tabs:
                self._update_moderation_available(tab)
                tab.header.set_youtube_send_enabled(False, detail)
        for tab in self._tabs:
            tab.add_message(Message.system(
                Platform.TWITCH if platform == "twitch" else Platform.SYSTEM,
                tab.twitch_channel, f"{platform}: {detail}"))

    # -- send path ------------------------------------------------------------------------

    def _queue_send(self, tab: Tab, text: str) -> None:
        if self._send is None:
            return
        reply = tab.consume_reply_prefix(text)  # banner up + "@Name " intact
        if reply is not None:
            rest, parent_id, parent_login = reply
            self._send_text(tab, rest, reply_parent_id=parent_id,
                            reply_parent_login=parent_login)
            return
        if text[:1] in ("/", "."):
            self._run_command(tab, text)
            return
        self._send_text(tab, text)

    def _on_reply_requested(self, tab: Tab, msg: Message) -> None:
        """Context-menu Reply: Twitch sets the reply banner; YouTube has no
        reply protocol, so a plain '@Name ' mention goes into the input."""
        if msg.author is None or msg.is_system:
            return
        name = msg.author.name or msg.author.login
        if msg.platform is not Platform.TWITCH:
            tab.input.insert_mention(name)
            return
        tab.show_reply(msg.id, (msg.author.login or name).lower(), name)

    def _on_search_requested(self, tab: Tab, selection: str, all_tabs: bool) -> None:
        """Ctrl+F: snapshot every visible message ONCE, then open the popup
        (messages arriving after open are excluded — parity)."""
        tabs = self._tabs if all_tabs else [tab]
        entries: list[tuple[Tab, Message]] = [
            (t, msg)
            for t in tabs
            for msg in t.view.message_model.all_messages()
        ]
        if self._search_popup is not None:
            self._search_popup.close()
        self._search_popup = SearchPopup(self._settings, entries, selection, parent=self)
        self._search_popup.go_to_requested.connect(self._on_search_go_to)
        self._search_popup.closed.connect(self._on_search_closed)
        self._search_popup.show_near(self)
        self._search_popup.focus_input()

    def _on_search_go_to(self, tab: Tab, msg: Message) -> None:
        tab.view.scroll_to_message(msg.id)

    def _on_search_closed(self) -> None:
        self._search_popup = None

    # -- mentions ------------------------------------------------------------------------

    def _open_mentions(self) -> None:
        """/mentions and View > Mentions: the highlight store, newest first."""
        if self._highlights is None:
            return
        entries: list[tuple[Tab, Message]] = []
        for msg in self._highlights.messages():
            for tab in self._tabs_for_message(msg):
                entries.append((tab, msg))
        if self._mentions_popup is not None:
            self._mentions_popup.close()
        self._mentions_popup = MentionsPopup(self._settings, entries, parent=self)
        self._mentions_popup.go_to_requested.connect(self._on_mentions_go_to)
        self._mentions_popup.clear_requested.connect(self._on_mentions_clear)
        self._mentions_popup.closed.connect(self._on_mentions_closed)
        self._mentions_popup.show_near(self)

    def _tabs_for_message(self, msg: Message) -> list[Tab]:
        """Tabs a message would route to (mirrors _on_bridge_message)."""
        if msg.platform is Platform.YOUTUBE:
            return [tab for tab in self._tabs
                    if (msg.channel_id and tab.youtube_live_chat_id == msg.channel_id)
                    or msg.extra.get("watch_key") == tab.tab_key]
        return [tab for tab in self._tabs if tab.twitch_channel == msg.channel_id]

    def _on_highlighted(self, msg: Message) -> None:
        """Live append to the open /mentions popup."""
        if self._mentions_popup is None:
            return
        for tab in self._tabs_for_message(msg):
            self._mentions_popup.append(tab, msg)

    def _on_mentions_go_to(self, tab: Tab, msg: Message) -> None:
        tab.view.scroll_to_message(msg.id)

    def _on_mentions_clear(self) -> None:
        if self._highlights is not None:
            self._highlights.clear()
        if self._mentions_popup is not None:
            self._mentions_popup.clear()

    def _on_mentions_closed(self) -> None:
        self._mentions_popup = None

    # -- autocomplete + emote picker -------------------------------------------------

    def _completion_source_for(self, tab: Tab):
        """Per-tab (text, cursor) -> CompletionResult, built from the chatter
        index (Twitch snapshot + YouTube chatters), the emote registry and the
        command tables. `:` degrades to emoji-only without a Twitch channel."""
        def source(text: str, cursor: int):
            users = self._chatters_for(tab)
            emotes = (self._twitch.registry.all_emotes()
                      if self._twitch is not None and tab.twitch_channel else [])
            commands = []
            if self._commands is not None:
                commands = self._commands.builtin_names()
                commands += [f"/{cmd.name}" for cmd in self._commands.user_commands]
            return build_candidates(
                text, cursor, users=users, emotes=emotes, commands=commands,
                emojis=emoji_dataset(), reply_prefix=tab.reply_prefix())

        return source

    def _open_emote_picker_for_current(self) -> None:
        widget = self._notebook.currentWidget()
        if isinstance(widget, Tab):
            self._open_emote_picker(widget)

    def _open_emote_picker(self, tab: Tab) -> None:
        if self._twitch is None:
            return
        if self._emote_picker is not None:
            self._emote_picker.close()
        picker = EmotePickerPopup(
            self._settings, self._twitch.registry,
            default_tab="Subs" if tab.twitch_channel else "Emojis", parent=self)
        picker.insert_requested.connect(functools.partial(self._on_picker_insert, tab))
        picker.closed.connect(self._on_emote_picker_closed)
        self._emote_picker = picker
        picker.show_above(tab.input)

    def _on_picker_insert(self, tab: Tab, text: str) -> None:
        tab.input.insert_text(text)

    def _on_emote_picker_closed(self) -> None:
        self._emote_picker = None

    def _run_command(self, tab: Tab, text: str) -> None:
        """Dispatch '/'- and '.'-prefixed input; unhandled (e.g. /me) sends."""
        if self._commands is None:
            self._send_text(tab, text)
            return
        outcome = self._commands.dispatch(text, self._command_context(tab))
        if not outcome.handled:
            self._send_text(tab, text)  # /me and .me pass through to Twitch
            return
        if outcome.system:
            tab.add_message(Message.system(
                outcome.system_platform,
                self._system_channel(tab, outcome.system_platform),
                outcome.system))
        if outcome.send_text:
            self._send_text(
                tab, outcome.send_text,
                reply_parent_id=outcome.reply_parent_id or None,
                reply_parent_login=outcome.reply_parent_login or None,
                force_twitch_only=outcome.twitch_only)

    def _command_context(self, tab: Tab) -> CommandContext:
        author = self._twitch.self_author() if self._twitch is not None else None
        return CommandContext(
            twitch_channel=tab.twitch_channel,
            youtube_live_chat_id=tab.youtube_live_chat_id,
            my_login=author.login if author else "",
            my_name=author.name if author else "",
            find_last_from=functools.partial(self._find_last_from, tab),
            clear_view=tab.view.message_model.clear,
            copy_text=lambda text: QApplication.clipboard().setText(text),
            open_url=lambda url: QDesktopServices.openUrl(QUrl(url)),
            open_mentions=self._open_mentions,
            list_chatters=lambda: self._chatters_for(tab),
            open_usercard=lambda login: self._open_usercard_by_login(tab, login),
        )

    def _chatters_for(self, tab: Tab) -> list[str]:
        """Current chatter names for the pair (Twitch + YouTube, deduped)."""
        users: list[str] = []
        if self._chat_index is not None:
            users = self._chat_index.users(tab.twitch_channel)
            if tab.youtube_live_chat_id:
                seen = {name.lower() for name in users}
                for name in self._chat_index.users(tab.youtube_live_chat_id):
                    if name.lower() not in seen:
                        seen.add(name.lower())
                        users.append(name)
        return users

    def _open_usercard_by_login(self, tab: Tab, login: str) -> None:
        """/usercard: open the card from a name (last matching message, or a
        synthesized author when the chatter has no rows in the scrollback)."""
        msg = self._find_last_from(tab, login)
        if msg is None:
            if tab.youtube_live_chat_id and not tab.twitch_channel:
                # YouTube-only tab: the card must take the YouTube branch
                # (Ban/Unban/Timeout/Delete) instead of a Twitch layout.
                msg = Message.text(Platform.YOUTUBE, tab.youtube_live_chat_id,
                                   Author(name=login, login=login), "")
            else:
                msg = Message.text(Platform.TWITCH, tab.twitch_channel,
                                   Author(name=login, login=login), "")
        self._show_usercard(msg, login)

    def _find_last_from(self, tab: Tab, login: str) -> Message | None:
        for msg in reversed(tab.view.message_model.all_messages()):
            if msg.author is not None and msg.author.login.lower() == login:
                return msg
        return None

    def _send_text(self, tab: Tab, text: str, *, reply_parent_id: str | None = None,
                   reply_parent_login: str | None = None,
                   force_twitch_only: bool = False) -> None:
        if self._send is None:
            return
        tab.hide_offer()
        pending_offer = self._offers.pop(tab.tab_key, None)
        if pending_offer is not None:
            self._send.discard_request(pending_offer[0])  # banner hidden: offer void
        tab.input.clear_send_chips()

        youtube_enabled = tab.header.youtube_enabled() and not force_twitch_only
        # force_twitch_only (moderation commands) bypasses the Twitch send
        # toggle: the shield/action implies the mod command must go out even
        # while the user keeps personal sends to one platform.
        twitch_enabled = tab.header.twitch_enabled() or force_twitch_only
        if youtube_enabled and not tab.youtube_live_chat_id:
            tab.add_message(Message.system(
                Platform.YOUTUBE, "",
                "no live chat connected yet — message not sent to YouTube",
                extra={"watch_key": tab.tab_key}))
            youtube_enabled = False  # never silently drop: the row above says why

        if not twitch_enabled and not youtube_enabled:
            tab.add_message(Message.system(
                Platform.SYSTEM, tab.twitch_channel,
                "nothing to send to — enable a send toggle"))
            return

        targets = SendTargets(
            twitch_channel=tab.twitch_channel,
            youtube_live_chat_id=tab.youtube_live_chat_id,
            twitch_enabled=twitch_enabled,
            youtube_enabled=youtube_enabled,
        )
        optimistic = self._send.send(
            tab.tab_key, text, targets,
            reply_parent_id=reply_parent_id,
            reply_parent_login=reply_parent_login)
        for msg in optimistic.values():
            tab.add_message(msg)

    def _on_send_result(self, result: SendResult) -> None:
        tab = self._tab_by_key(result.tab_key)
        if tab is None:
            return
        label = _PLATFORM_LABEL.get(result.platform, "?")
        if result.state == "validation_failed":
            tab.input.show_send_chip(_chip_name(result.platform), "blocked", "error")
            if result.offer is not None:
                self._offers[result.tab_key] = (result.key, result.offer)
                tab.show_offer(
                    f"[{label}] rejected: {result.error}", result.offer_label)
            else:
                tab.add_message(Message.system(
                    result.platform, self._system_channel(tab, result.platform),
                    f"{label} rejected: {result.error}",
                    extra={"watch_key": tab.tab_key}))
        elif result.state == "queued":
            tab.input.show_send_chip(_chip_name(result.platform), "⏳ queued…", "pending")
        elif result.state == "sending":
            tab.input.show_send_chip(_chip_name(result.platform), "⏳ sending…", "pending")
        elif result.state == "retrying":
            tab.input.show_send_chip(
                _chip_name(result.platform),
                f"⏳ retrying in {result.retry_after:.0f} s", "pending",
                cancel=functools.partial(self._send.cancel, result.key, result.platform))
        elif result.state == "sent":
            suffix = " (truncated)" if result.truncated else ""
            tab.input.show_send_chip(_chip_name(result.platform), f"✓{suffix}", "ok")
        elif result.state == "failed":
            tab.input.show_send_chip(_chip_name(result.platform), "failed", "error")
            if result.message_id:
                tab.view.message_model.mark_deleted(result.message_id)
            tab.add_message(Message.system(
                result.platform, self._system_channel(tab, result.platform),
                f"{label} send failed: {result.error}",
                extra={"watch_key": tab.tab_key}))
        elif result.state == "cancelled":
            tab.input.hide_send_chip(_chip_name(result.platform))
            if result.message_id:
                tab.view.message_model.mark_deleted(result.message_id)

    def _accept_offer(self, tab: Tab) -> None:
        pending = self._offers.pop(tab.tab_key, None)
        if pending is None or self._send is None:
            return
        request_key, platform = pending
        tab.hide_offer()
        msg = self._send.send_offer(request_key, platform)
        if msg is not None:
            tab.add_message(msg)

    @staticmethod
    def _system_channel(tab: Tab, platform: Platform) -> str:
        if platform is Platform.TWITCH:
            return tab.twitch_channel
        return tab.youtube_live_chat_id or tab.youtube_video_id or ""

    # -- YouTube account ---------------------------------------------------------------------

    def _connect_youtube(self) -> None:
        if self._youtube is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Select client_secrets.json", "", "JSON files (*.json)")
        if not path:
            return
        asyncio.ensure_future(self._run_youtube_auth(path))

    async def _run_youtube_auth(self, path: str) -> None:
        assert self._youtube is not None
        try:
            with open(path, encoding="utf-8") as fh:
                self._youtube.auth.set_client_secrets(json.load(fh))
            auth_url = await self._youtube.auth.begin_flow()
        except (YouTubeAuthError, OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "YouTube", f"Could not load client_secrets.json:\n{exc}")
            return
        QDesktopServices.openUrl(QUrl(auth_url))
        self.statusBar().showMessage("Complete sign-in in your browser…")
        ok = await self._youtube.auth.wait_for_code()
        self._youtube.tokens = await self._youtube.auth.ensure_valid()
        if not ok or self._youtube.tokens is None:
            bridge.auth_state.emit("youtube", False, "sign-in failed or timed out")
            return
        bridge.auth_state.emit("youtube", True, "connected")
        await self._youtube.load_identity()
        self._refresh_youtube_ownership()
        self.statusBar().showMessage("YouTube account connected", 5000)

    # -- Twitch account ---------------------------------------------------------------------

    def _connect_twitch(self) -> None:
        if self._twitch is not None:
            asyncio.ensure_future(self._run_twitch_auth())

    async def _run_twitch_auth(self) -> None:
        assert self._twitch is not None
        try:
            auth_url = await self._twitch.auth.begin_flow()
        except TwitchAuthError as exc:
            QMessageBox.warning(self, "Twitch", str(exc))
            return
        QDesktopServices.openUrl(QUrl(auth_url))
        self.statusBar().showMessage("Complete Twitch sign-in in your browser…")
        try:
            tokens = await self._twitch.auth.wait_for_code()
        except TwitchAuthError as exc:
            QMessageBox.warning(self, "Twitch", f"Twitch sign-in failed: {exc}")
            return
        except Exception as exc:  # network hiccup in validate/exchange plumbing
            log.exception("Twitch sign-in failed after the browser callback")
            QMessageBox.warning(
                self, "Twitch",
                f"Twitch sign-in failed after the browser callback: {exc}")
            return
        if tokens is None:
            bridge.auth_state.emit("twitch", False, "sign-in failed or timed out")
            return
        # Fresh IRC connection on the new identity. reconnect() returns
        # False when the token refresh failed and it kept the old
        # connection — it has already emitted auth_state(False), so the
        # status bar must not overwrite that with "connected". The success
        # emit fires only AFTER the swap: the token check inside reconnect
        # takes several network round trips, and announcing "authenticated"
        # before it completes lies while a refresh is failing.
        try:
            connected = await self._twitch.reconnect()
        except Exception as exc:
            log.exception("Twitch reconnect failed after sign-in")
            QMessageBox.warning(
                self, "Twitch", f"Twitch connection failed after sign-in: {exc}")
            return
        if connected:
            bridge.auth_state.emit("twitch", True, "connected")
            self.statusBar().showMessage("Twitch account connected", 5000)

    # -- tabs ------------------------------------------------------------------------

    def _tab_by_key(self, tab_key: str) -> Tab | None:
        for tab in self._tabs:
            if tab.tab_key == tab_key:
                return tab
        return None

    def add_tab(self, twitch_channel: str, youtube_target: str | None) -> Tab:
        twitch_channel = twitch_channel.strip().lstrip("#@").lower()
        youtube_target = (youtube_target or "").strip()
        tab = Tab(self._settings, twitch_channel, youtube_target)
        self._tabs.append(tab)
        title = _tab_title(twitch_channel, youtube_target)
        self._notebook.add_chat_tab(tab, title)
        tab.usercard_requested.connect(self._show_usercard)
        tab.reply_requested.connect(functools.partial(self._on_reply_requested, tab))
        tab.search_requested.connect(functools.partial(self._on_search_requested, tab))
        tab.offer_accept.connect(functools.partial(self._accept_offer, tab))
        tab.offer_edit.connect(tab.input.focus_input)
        tab.header.send_toggle_changed.connect(
            functools.partial(self._on_send_toggle, tab))
        tab.header.moderation_toggled.connect(
            functools.partial(self._on_moderation_toggled, tab))
        tab.mod_action_requested.connect(functools.partial(self._on_mod_action, tab))
        tab.delete_requested.connect(functools.partial(self._on_delete_requested, tab))
        tab.pin_requested.connect(functools.partial(self._on_pin_requested, tab))
        tab.view.set_moderation_actions(self._mod_action_names())
        tab.view.set_youtube_actions(["Ban", "Timeout", "Hide"])
        self._update_moderation_available(tab)
        # Replay the cached chat-mode label: an already-joined channel gets
        # no fresh ROOMSTATE (the join is a no-op), but the header must
        # still show the current modes.
        tab.header.set_chat_modes(self._roomstate_text.get(twitch_channel, ""))
        self._sync_send_limits(tab)
        self._sync_empty_state()

        if self._demo:
            feed = FakeFeed(tab, twitch_channel or "forsen", "Demo Stream")
            self._feeds.append(feed)
            tab.set_send_handler(feed.handle_send)
            feed.start()
        else:
            tab.set_send_handler(functools.partial(self._queue_send, tab))
            tab.input.set_completion_source(self._completion_source_for(tab))
            tab.input.emote_picker_requested.connect(
                functools.partial(self._open_emote_picker, tab))
            asyncio.ensure_future(self._join_twitch(tab))
            if self._youtube is not None and youtube_target:
                tab.youtube_target = youtube_target
                self._youtube.watch(youtube_target, tab.tab_key)
        return tab

    async def _join_twitch(self, tab: Tab) -> None:
        if self._twitch is None or not tab.twitch_channel:
            return
        if not await self._twitch.join_channel(tab.twitch_channel):
            tab.add_message(Message.system(
                Platform.TWITCH, tab.twitch_channel,
                "Twitch join limit reached — channel not joined"))

    def _on_send_toggle(self, tab: Tab, platform: str, enabled: bool) -> None:
        # Dual sends cap both platforms at YouTube's 200; Twitch-only
        # restores the 500-codepoint counter.
        self._sync_send_limits(tab)

    def _sync_send_limits(self, tab: Tab) -> None:
        dual = tab.header.twitch_enabled() and tab.header.youtube_enabled()
        tab.input.set_twitch_limit(YOUTUBE_MESSAGE_LIMIT if dual else TWITCH_MESSAGE_LIMIT)

    def _new_streamer_tab(self) -> Tab:
        """Stream tab: the signed-in user's own Twitch channel chat (joinable
        while offline) + the live chat of their current YouTube stream, which
        auto-attaches when they go live (waiting placeholder before that)."""
        tab = self.add_tab("", MINE_TARGET)
        self._streamer_tabs.append(tab)
        tab.set_youtube("Your Stream")  # don't leak the "mine:" sentinel into the header
        tab.set_youtube_status("waiting for your live stream…")
        self._bind_streamer_tabs()  # binds immediately if Twitch is already connected
        return tab

    def _bind_streamer_tabs(self) -> None:
        """Point every unbound Stream tab's Twitch side at the signed-in
        login. Idempotent (already-bound tabs are skipped) and sticky: a
        later Twitch disconnect keeps the channel (anonymous IRC re-joins it
        read-only) and the title keeps the login."""
        login = ""
        if self._twitch is not None and self._twitch.tokens is not None:
            login = self._twitch.tokens.login
        if not login:
            return
        for tab in self._streamer_tabs:
            if tab.twitch_channel:
                continue
            tab.set_twitch(login)
            self._retitle_tab(tab)
            asyncio.ensure_future(self._join_twitch(tab))

    def _retitle_tab(self, tab: Tab) -> None:
        index = self._notebook.indexOf(tab)
        if index >= 0:
            self._notebook.setTabText(
                index, _tab_title(tab.twitch_channel, tab.youtube_target))

    def _rewatch_streamer_tabs(self) -> None:
        """Restart every Stream tab's mine-watch after YouTube (re)auth: the
        running watch loop would otherwise keep polling with its old token
        for up to the full sleep interval."""
        if self._youtube is None:
            return
        for tab in self._streamer_tabs:
            self._youtube.unwatch(tab.tab_key)
            self._youtube.watch(tab.youtube_target, tab.tab_key)

    def _new_twitch_tab(self) -> None:
        twitch, ok = QInputDialog.getText(self, "New Twitch Tab", "Twitch channel:")
        if not ok:
            return
        twitch = twitch.strip().lstrip("#@").lower()
        self.add_tab(twitch or "forsen", None)

    def _new_youtube_tab(self) -> None:
        youtube, ok = QInputDialog.getText(
            self, "New YouTube Tab",
            "YouTube live video URL / video ID / @handle:")
        if not ok:
            return
        youtube = youtube.strip()
        if not youtube:  # a tab with no chats at all would be useless
            return
        self.add_tab("", youtube)

    def _sync_empty_state(self) -> None:
        """Show the link page in place of an empty notebook, and vice versa."""
        self._stack.setCurrentWidget(
            self._empty_tab_page if self._notebook.count() == 0 else self._notebook)

    def _close_tab(self, index: int) -> None:
        if index < 0:
            return
        widget = self._notebook.widget(index)
        self._notebook.removeTab(index)
        if isinstance(widget, Tab) and widget in self._tabs:
            self._tabs.remove(widget)
            if widget in self._streamer_tabs:
                self._streamer_tabs.remove(widget)
            self._offers.pop(widget.tab_key, None)
            still_joined = widget.twitch_channel and any(
                tab.twitch_channel == widget.twitch_channel for tab in self._tabs)
            if not still_joined:
                if self._twitch is not None and widget.twitch_channel:
                    self._twitch.part(widget.twitch_channel)  # only part when no other tab joins it
                if widget.twitch_channel:
                    # The chat-mode label cache is stale once nothing joins
                    # the channel: modes may change while unjoined, and a
                    # refused re-join (90-channel cap) would replay the old
                    # label forever.
                    self._roomstate_text.pop(widget.twitch_channel, None)
            if self._youtube is not None:
                self._youtube.unwatch(widget.tab_key)
        # Drop feeds for closed tabs (stop their tasks).
        remaining: list[FakeFeed] = []
        for feed in self._feeds:
            if feed.tab is widget:
                feed.stop()
            else:
                remaining.append(feed)
        self._feeds = remaining
        self._sync_empty_state()

    # -- actions -----------------------------------------------------------------------

    def _toggle_theme(self) -> None:
        current = self._settings.get("theme", "dark")
        new_theme = "light" if current == "dark" else "dark"
        self._settings.set("theme", new_theme)
        theme.apply(QApplication.instance(), new_theme, self._settings.get("ui_scale", 1.0))
        self._settings.save()

    def _zoom(self, factor: float, reset: bool = False) -> None:
        scale = 1.0 if reset else self._settings.get("ui_scale", 1.0) * factor
        self._settings.set("ui_scale", scale)
        theme.apply(QApplication.instance(), self._settings.get("theme", "dark"), scale)
        self._settings.save()

    def _on_theme_changed(self) -> None:
        for tab in self._tabs:
            tab.refresh_visuals()

    def _show_usercard(self, msg: Message, login: str) -> None:
        """Username click: card at the cursor, Helix/YouTube data pushed in async."""
        if msg.author is None:
            return
        tabs = self._tabs_for_message(msg)
        if not tabs:
            return
        tab = tabs[0]
        author = msg.author
        me = self._twitch.self_author() if self._twitch is not None else None
        if msg.platform is Platform.YOUTUBE:
            # The Twitch self login is meaningless for YouTube rows: self =
            # the connected account's own channel id (docs/m4.md: mod buttons
            # never target yourself).
            my_id = self._youtube.self_channel_id() if self._youtube is not None else ""
            is_self = bool(my_id and author.user_id
                           and my_id.lower() == author.user_id.lower())
        else:
            is_self = bool(me and (me.login or "").lower() == (author.login or login).lower())
        target = UsercardTarget(
            platform=msg.platform,
            login=(author.login or login).lower() or login,
            display_name=author.name or login,
            user_id=author.user_id,
            avatar_url=author.avatar_url,
            color=author.color,
            is_self=is_self,
            channel_id=msg.channel_id,
        )
        recent = [(tab, m) for m in
                  recent_messages_for(tab.view.message_model.all_messages(), target.login)]
        if self._usercard_popup is not None:
            self._usercard_popup.close()
        if msg.platform is Platform.TWITCH:
            self_is_mod = bool(tab.twitch_channel) and self._moderation.has_twitch_rights(
                tab.twitch_channel)
        else:
            self_is_mod = bool(tab.youtube_live_chat_id) and self._moderation.has_youtube_rights(
                tab.youtube_live_chat_id)
        popup = UsercardPopup(
            self._settings, target, recent, parent=self,
            self_is_mod=self_is_mod,
            custom_actions=self._settings.get("moderation_actions", []),
        )
        popup.action_requested.connect(
            functools.partial(self._on_usercard_action, tab, target))
        popup.closed.connect(self._on_usercard_closed)
        self._usercard_popup = popup
        popup.show_at_cursor()
        asyncio.ensure_future(self._enrich_usercard(popup, target))

    def _on_usercard_action(self, tab: Tab, target: UsercardTarget, text: str) -> None:
        if target.platform is Platform.TWITCH:
            # /ban /unban /timeout ... go out as IRC PRIVMSG mod commands
            # (Chatterino's own mechanism); NOTICE msg-ids give feedback.
            self._run_command(tab, text)
            return
        cmd = text.split()[0] if text.split() else ""
        if not target.user_id and cmd != "/delete":
            # Server-side actions need the channel id (liveChatBans can't
            # look users up by name); /delete is a local hide that only
            # needs the login.
            self._yt_note(tab, f"no channel id recorded for {target.display_name} — action skipped")
            return
        if cmd == "/ban":
            asyncio.ensure_future(self._yt_ban(tab, target.user_id))
        elif cmd == "/unban":
            asyncio.ensure_future(self._yt_unban(tab, target.user_id, target.display_name))
        elif cmd == "/timeout":
            asyncio.ensure_future(self._yt_ban(tab, target.user_id, duration=300))
        elif cmd == "/delete":
            # The chat the card's row came from (falls back to the tab id
            # for synthesized cards): rows survive a UNAVAILABLE/ENDED that
            # clears the tab id, and the local hide must keep working.
            self._yt_hide_user(tab, target.channel_id or tab.youtube_live_chat_id,
                               target.login, target.display_name)

    def _on_usercard_live(self, msg: Message) -> None:
        """Recent-messages live append while the card is open."""
        popup = self._usercard_popup
        if popup is None or msg.author is None:
            return
        if (msg.author.login or "").lower() != popup.target.login.lower():
            return
        for tab in self._tabs_for_message(msg):
            popup.append_recent((tab, msg))
            break

    def _on_usercard_closed(self) -> None:
        self._usercard_popup = None

    async def _enrich_usercard(self, popup: UsercardPopup, target: UsercardTarget) -> None:
        """Helix/YouTube field expansion; the popup may have closed meanwhile."""
        try:
            if target.platform is Platform.TWITCH:
                await self._enrich_twitch_usercard(popup, target)
            else:
                await self._enrich_youtube_usercard(popup, target)
        except Exception as exc:  # enrichment is best-effort
            log.debug("usercard enrichment failed: %s", exc)

    async def _enrich_twitch_usercard(self, popup: UsercardPopup,
                                      target: UsercardTarget) -> None:
        if self._twitch is None or self._twitch.helix is None:
            return  # anonymous read: Author fields only
        user = await self._twitch.helix.user_by_login(target.login)
        if user is None or self._usercard_popup is not popup:
            return
        followers = followage = None
        broadcaster_id = user.get("id") or target.user_id
        if broadcaster_id:
            followers = await self._twitch.helix.channel_followers(broadcaster_id)
            me = self._twitch.self_author()
            if me and me.user_id and me.user_id != broadcaster_id:
                followage = await self._twitch.helix.user_follows(me.user_id, broadcaster_id)
        if self._usercard_popup is not popup:
            return
        popup.set_details(
            created_at=user.get("created_at", ""),
            followers=followers,
            followage=followage or "",
            avatar_url=user.get("profile_image_url") or None,
        )

    async def _enrich_youtube_usercard(self, popup: UsercardPopup,
                                       target: UsercardTarget) -> None:
        if self._youtube is None or not target.user_id:
            return
        try:
            data = await self._youtube.api.channel_by_id(target.user_id)
        except Exception:  # no OAuth/API key: skip silently
            return
        if data is None or self._usercard_popup is not popup:
            return
        items = data.get("items", [])
        if not items:
            return
        item = items[0]
        subscribers = str(item.get("statistics", {}).get("subscriberCount") or "")
        popup.set_details(
            created_at=item.get("snippet", {}).get("publishedAt", ""),
            followers=int(subscribers) if subscribers.isdigit() else None,
        )

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About ChatterSyn",
            "ChatterSyn — a Chatterino-style chat client for Twitch + YouTube live chat.\n\n"
            "One input, two chats. Built with Python + PySide6.",
        )

    def closeEvent(self, event) -> None:
        for feed in self._feeds:
            feed.stop()
        self._save_geometry()
        super().closeEvent(event)


def _chip_name(platform: Platform) -> str:
    return "twitch" if platform is Platform.TWITCH else "youtube"
