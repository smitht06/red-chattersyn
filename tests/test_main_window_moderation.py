"""MainWindow moderation wiring: ClearChat "(N times)" stacking, USERSTATE
shield gating, YouTube owner checks, row/context-menu moderation actions.

All tests are async: the demo-mode MainWindow's FakeFeed starts an asyncio
task, which needs a running loop (pytest-asyncio auto mode).
"""

import asyncio
import types

import pytest
from PySide6.QtCore import QSettings

from chattersyn.config.settings import Settings
from chattersyn.models.message import Author, Badge, Message, MessageFlags, Platform
from chattersyn.services.twitch.mapper import ClearChat, RoomState, UserState
from chattersyn.services.youtube.service import WatchState, YoutubeState
from chattersyn.ui.main_window import MainWindow
from chattersyn.ui.usercard import UsercardTarget


@pytest.fixture
async def win(qapp, tmp_path):
    # Temp-file QSettings: geometry save/restore must not touch the real
    # user prefs when tests close the window.
    w = MainWindow(Settings({}), demo=True,
                   qsettings=QSettings(str(tmp_path / "qt.ini"), QSettings.Format.IniFormat))
    for feed in list(w._feeds):  # silence the demo feed generator
        feed.stop()
    w._feeds.clear()
    yield w
    w.close()


def _tw_msg(channel="forsen", login="bob", name="Bob"):
    return Message.text(Platform.TWITCH, channel, Author(name=name, login=login), "hello")


def _system_rows(win):
    return [m for m in win._tabs[0].view.message_model.all_messages() if m.is_system]


# -- ClearChat stacking -----------------------------------------------------------


async def test_clearchat_grays_rows_and_stacks(win):
    tab = win._tabs[0]
    tab.add_message(_tw_msg())
    tab.add_message(_tw_msg())
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    tab.view._flush()

    rows = tab.view.message_model.all_messages()
    user_rows = [m for m in rows if m.author is not None]
    assert user_rows and all(m.flags & MessageFlags.DISABLED for m in user_rows)
    assert len(_system_rows(win)) == 1
    assert _system_rows(win)[0].plain == "Bob was timed out for 600s. (2 times)"


async def test_clearchat_stack_restarts_when_user_posts_again(win):
    tab = win._tabs[0]
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    tab.view._flush()
    win._on_bridge_message(_tw_msg())  # the user posts again: fresh stack
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    tab.view._flush()

    assert [m.plain for m in _system_rows(win)] == [
        "Bob was timed out for 600s.", "Bob was timed out for 600s."]


async def test_clearchat_stack_expires_after_window(win, monkeypatch):
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    monkeypatch.setattr("chattersyn.ui.main_window.time.monotonic", lambda: 1e9)
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    win._tabs[0].view._flush()
    assert len(_system_rows(win)) == 2  # outside 5s window: a fresh row


async def test_clearchat_stack_breaks_after_20_messages(win):
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    for i in range(21):
        win._on_bridge_message(_tw_msg(login=f"user{i}", name=f"User{i}"))
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    win._tabs[0].view._flush()
    assert len(_system_rows(win)) == 2


async def test_clearchat_full_clear_grays_all_rows(win):
    """Spec: "'Chat has been cleared by a moderator.' + disable all" — rows
    stay visible but grayed (scrollback survives for search/copy)."""
    tab = win._tabs[0]
    tab.add_message(_tw_msg())
    tab.add_message(_tw_msg())
    tab.view._flush()
    win._on_bridge_event(ClearChat(channel="forsen", user=None))
    tab.view._flush()
    rows = tab.view.message_model.all_messages()
    assert len(rows) == 3
    assert all(m.flags & MessageFlags.DISABLED for m in rows if m.author is not None)
    assert "cleared by a moderator" in rows[-1].plain


# -- shield gating ------------------------------------------------------------------


async def test_userstate_mod_enables_shield(win):
    tab = win._tabs[0]
    assert not tab.header._shield.isEnabled()
    win._on_user_state(UserState(channel="#forsen", mod=True, badges=[]))
    assert tab.header._shield.isEnabled()


async def test_self_badges_enable_shield(win):
    tab = win._tabs[0]
    win._on_user_state(UserState(channel="#forsen", mod=False,
                                 badges=[Badge("moderator", "1")]))
    assert tab.header._shield.isEnabled()


async def test_youtube_owner_enables_shield(win):
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    win._moderation.set_youtube_owner("lc1", True)
    win._update_moderation_available(tab)
    assert tab.header._shield.isEnabled()
    win._moderation.set_youtube_owner("lc1", False)
    win._update_moderation_available(tab)
    assert not tab.header._shield.isEnabled()


async def test_shield_toggle_switches_delegate(win):
    tab = win._tabs[0]
    win._on_user_state(UserState(channel="#forsen", mod=True, badges=[]))
    tab.header._shield.setChecked(True)  # toggled -> moderation_toggled fires
    assert tab.view._delegate._moderation_enabled


# -- row actions --------------------------------------------------------------------


async def test_mod_action_expands_placeholders_and_runs(win, monkeypatch):
    tab = win._tabs[0]
    ran = []
    monkeypatch.setattr(win, "_run_command", lambda t, text: ran.append(text))
    monkeypatch.setattr(
        win._settings, "get",
        lambda key, default=None: ([{"name": "Ban", "template": "/ban {user.name}"}]
                                   if key == "moderation_actions" else default))
    tab.view.set_moderation_actions(win._mod_action_names())
    msg = _tw_msg()
    win._on_mod_action(tab, msg, "Ban")
    assert ran == ["/ban Bob"]


async def test_delete_requested_youtube_hides_one_row(win):
    tab = win._tabs[0]
    msg = Message.text(Platform.YOUTUBE, "lc1", Author("bad", login="bad"), "spam")
    tab.add_message(msg)
    tab.view._flush()
    win._on_delete_requested(tab, msg)
    assert msg.flags & MessageFlags.DISABLED


async def test_delete_requested_twitch_is_deferred(win):
    tab = win._tabs[0]
    win._on_delete_requested(tab, _tw_msg())
    tab.view._flush()
    assert "channel:moderate" in _system_rows(win)[-1].plain


async def test_pin_requested_shows_banner(win):
    tab = win._tabs[0]
    win._on_pin_requested(tab, _tw_msg())
    assert not tab._pin_banner.isHidden()
    tab.unpin()
    assert tab._pin_banner.isHidden()


async def test_hide_action_disables_users_rows(win):
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    author = Author("bad", login="bad", user_id="UCbad")
    tab.add_message(Message.text(Platform.YOUTUBE, "lc1", author, "spam"))
    tab.add_message(Message.text(Platform.YOUTUBE, "lc1", author, "more"))
    tab.view._flush()
    msg = Message.text(Platform.YOUTUBE, "lc1", author, "spam")
    win._on_youtube_mod_action(tab, msg, "Hide")
    tab.view._flush()
    rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
    assert len(rows) == 2 and all(m.flags & MessageFlags.DISABLED for m in rows)


class _FakeYtApi:
    def __init__(self):
        self.bans: list[tuple] = []
        self.unbans: list[str] = []

    async def live_chat_ban(self, live_chat_id, channel_id, duration=0):
        self.bans.append((live_chat_id, channel_id, duration))
        return {"id": "ban-1"}

    async def live_chat_unban(self, ban_id):
        self.unbans.append(ban_id)
        return {}


@pytest.fixture
async def win_yt(win):
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    api = _FakeYtApi()
    win._youtube = types.SimpleNamespace(api=api, self_channel_id=lambda: "UCme")
    return win, tab, api


async def test_youtube_ban_records_ban_id(win_yt):
    win, tab, api = win_yt
    msg = Message.text(Platform.YOUTUBE, "lc1", Author("bad", login="bad", user_id="UCbad"), "hi")
    win._on_youtube_mod_action(tab, msg, "Ban")
    await asyncio.sleep(0)
    assert api.bans == [("lc1", "UCbad", 0)]
    assert win._moderation.youtube_ban_id("lc1", "UCbad") == "ban-1"


async def test_youtube_timeout_is_300s(win_yt):
    win, tab, api = win_yt
    msg = Message.text(Platform.YOUTUBE, "lc1", Author("bad", login="bad", user_id="UCbad"), "hi")
    win._on_youtube_mod_action(tab, msg, "Timeout")
    await asyncio.sleep(0)
    assert api.bans == [("lc1", "UCbad", 300)]


async def test_youtube_unban_deletes_remembered_ban(win_yt):
    win, tab, api = win_yt
    win._moderation.remember_youtube_ban("lc1", "UCbad", "ban-7")
    win._on_usercard_action(
        tab, UsercardTarget(Platform.YOUTUBE, "bad", "Bad", user_id="UCbad"),
        "/unban bad")
    await asyncio.sleep(0)
    assert api.unbans == ["ban-7"]
    assert win._moderation.youtube_ban_id("lc1", "UCbad") is None


async def test_usercard_youtube_delete_hides_locally(win):
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    author = Author("Bad", login="bad", user_id="UCbad")
    tab.add_message(Message.text(Platform.YOUTUBE, "lc1", author, "spam"))
    tab.add_message(Message.text(Platform.YOUTUBE, "lc1", author, "more"))
    tab.view._flush()
    win._on_usercard_action(
        tab, UsercardTarget(Platform.YOUTUBE, "bad", "Bad", user_id="UCbad"),
        "/delete bad")
    tab.view._flush()
    # The note AND the actual hiding must both happen (a no-op hide that
    # only writes the note must fail this test).
    rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
    assert len(rows) == 2 and all(m.flags & MessageFlags.DISABLED for m in rows)
    assert "Bad" in _system_rows(win)[-1].plain


# -- multi-tab bookkeeping (review: per-tab loop inflated shared state) ------------


async def test_clearchat_two_tabs_same_channel_stack_identically(win):
    tab1 = win._tabs[0]
    tab2 = win.add_tab("forsen", None)
    win._feeds[-1].stop()  # demo feed would inject rows into tab2
    for tab in (tab1, tab2):
        tab.add_message(_tw_msg())
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    for tab in (tab1, tab2):
        tab.view._flush()
        system = [m for m in tab.view.message_model.all_messages() if m.is_system]
        # Bookkeeping runs once per event: both tabs show the same count.
        assert len(system) == 1
        assert system[0].plain == "Bob was timed out for 600s. (2 times)"
        user_rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
        assert user_rows and all(m.flags & MessageFlags.DISABLED for m in user_rows)


async def test_clearchat_untimeout_ungrays_without_stacking(win):
    """/untimeout arrives as ban-duration=0: rows un-gray, no '(N times)'
    inflation, no permanent entry."""
    tab = win._tabs[0]
    tab.add_message(_tw_msg())
    tab.view._flush()
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=0))
    tab.view._flush()
    rows = tab.view.message_model.all_messages()
    user_rows = [m for m in rows if m.author is not None]
    assert user_rows and not any(m.flags & MessageFlags.DISABLED for m in user_rows)
    system = [m for m in rows if m.is_system]
    assert [m.plain for m in system] == ["Bob was timed out for 600s.", "Bob was unbanned."]


async def test_timeout_expiry_ungrays_rows(win, monkeypatch):
    """The DISABLED overlay clears when the timeout duration passes."""
    fired: list = []
    monkeypatch.setattr(
        "chattersyn.ui.main_window.QTimer.singleShot", lambda ms, cb: fired.append(cb))
    clock = {"now": 0.0}
    monkeypatch.setattr(
        "chattersyn.services.moderation.time.monotonic", lambda: clock["now"])
    tab = win._tabs[0]
    tab.add_message(_tw_msg())
    tab.view._flush()
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
    assert all(m.flags & MessageFlags.DISABLED for m in rows)
    assert len(fired) == 1
    clock["now"] = 601  # the timeout is over
    fired[0]()
    assert not any(m.flags & MessageFlags.DISABLED for m in rows)


async def test_timeout_expiry_skipped_when_newer_ban_active(win, monkeypatch):
    """A re-ban within the window replaces the deadline — the old timer
    fires but must not un-gray the active ban's rows."""
    fired: list = []
    monkeypatch.setattr(
        "chattersyn.ui.main_window.QTimer.singleShot", lambda ms, cb: fired.append(cb))
    clock = {"now": 0.0}
    monkeypatch.setattr(
        "chattersyn.services.moderation.time.monotonic", lambda: clock["now"])
    tab = win._tabs[0]
    tab.add_message(_tw_msg())
    tab.view._flush()
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=None))  # permaban
    tab.view._flush()
    assert len(fired) == 1  # only the timed entry scheduled a timer
    clock["now"] = 601
    fired[0]()  # the 600s timeout expired, but the ban is now permanent
    rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
    assert all(m.flags & MessageFlags.DISABLED for m in rows)


# -- shield re-evaluation -------------------------------------------------------------


async def test_userstate_demotion_disables_shield(win):
    tab = win._tabs[0]
    win._on_user_state(UserState(channel="#forsen", mod=True, badges=[]))
    assert tab.header._shield.isEnabled()
    tab.header._shield.setChecked(True)
    win._on_user_state(UserState(channel="#forsen", mod=False, badges=[]))
    assert not tab.header._shield.isEnabled()
    assert not tab.header._shield.isChecked()


async def test_tab_readd_reevaluates_shield(win):
    win._on_user_state(UserState(channel="#forsen", mod=True, badges=[]))
    win._close_tab(0)
    tab = win.add_tab("forsen", None)
    win._feeds[-1].stop()
    assert tab.header._shield.isEnabled()  # cached ModerationState still grants rights


# -- YouTube ownership / lifecycle ----------------------------------------------------


async def test_youtube_live_event_records_ownership(win):
    tab = win._tabs[0]
    win._youtube = types.SimpleNamespace(
        self_channel_id=lambda: "UCme",
        owns_channel=lambda cid: cid == "UCme",
        tokens="tok",
    )
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.LIVE,
        live_chat_id="lc1", channel_id="UCme", title="own stream"))
    assert win._moderation.has_youtube_rights("lc1") is True
    assert tab.header._shield.isEnabled()


async def test_youtube_ended_clears_stale_live_chat_id(win):
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    win._moderation.set_youtube_owner("lc1", True)
    win._moderation.remember_youtube_ban("lc1", "UCbad", "ban-1")
    win._update_moderation_available(tab)
    assert tab.header._shield.isEnabled()
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.ENDED))
    assert tab.youtube_live_chat_id == ""
    assert not tab.header._shield.isEnabled()
    assert not win._moderation.has_youtube_rights("lc1")
    assert win._moderation.youtube_ban_id("lc1", "UCbad") is None


async def test_youtube_ban_success_adds_no_rows(win_yt):
    """Server bans are announced by the live chat's own userBannedEvent —
    a success must not add an app row (double-reporting)."""
    win, tab, api = win_yt
    msg = Message.text(Platform.YOUTUBE, "lc1", Author("bad", login="bad", user_id="UCbad"), "hi")
    before = len(tab.view.message_model.all_messages())
    win._on_youtube_mod_action(tab, msg, "Ban")
    await asyncio.sleep(0)
    assert api.bans == [("lc1", "UCbad", 0)]
    assert len(tab.view.message_model.all_messages()) == before


async def test_usercard_self_youtube_hides_mod_buttons(win):
    """Your own YouTube rows must not offer Ban/Unban/Timeout/Delete."""
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    win._moderation.set_youtube_owner("lc1", True)
    win._youtube = types.SimpleNamespace(self_channel_id=lambda: "UCme")
    msg = Message.text(Platform.YOUTUBE, "lc1",
                       Author("Me", login="ucme", user_id="UCme"), "hello")
    tab.add_message(msg)
    tab.view._flush()
    win._show_usercard(msg, "ucme")
    popup = win._usercard_popup
    assert popup is not None
    assert popup.target.is_self
    assert popup._mod_frame.isHidden()
    popup.close()


async def test_mod_command_bypasses_twitch_toggle(win):
    """twitch_only moderation sends must go out even with the Twitch chip
    off — the shield implies the mod action runs."""
    class FakeSend:
        def __init__(self):
            self.calls = []

        def discard_request(self, key):
            pass

        def send(self, tab_key, text, targets, reply_parent_id=None, reply_parent_login=None):
            self.calls.append((text, targets.twitch_enabled, targets.youtube_enabled))
            return {}

    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    win._send = FakeSend()
    tab.header._twitch_chip.setChecked(False)  # user sends personally to YouTube only
    win._send_text(tab, "/ban bob", force_twitch_only=True)
    assert win._send.calls == [("/ban bob", True, False)]


async def test_search_snapshot_includes_disabled_rows(win):
    """Grayed-out rows stay in the Ctrl+F snapshot (visible-but-dimmed
    parity: search/copy see them)."""
    tab = win._tabs[0]
    msg = _tw_msg()
    tab.add_message(msg)
    tab.view._flush()
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    tab.view._flush()
    win._on_search_requested(tab, "", False)
    popup = win._search_popup
    assert popup is not None
    snapshot = [m for _, m in popup._entries]
    assert msg in snapshot and msg.flags & MessageFlags.DISABLED
    popup.close()


# -- timer semantics (verification pass: coarse timers fire early) --------------------


async def test_timeout_expiry_fires_early_still_ungrays(win, monkeypatch):
    """Coarse timers fire up to ~5% early — an early fire must still
    un-gray: the armed deadline, not the wall clock, decides."""
    fired: list = []
    monkeypatch.setattr(
        "chattersyn.ui.main_window.QTimer.singleShot", lambda ms, cb: fired.append(cb))
    clock = {"now": 0.0}
    monkeypatch.setattr(
        "chattersyn.services.moderation.time.monotonic", lambda: clock["now"])
    tab = win._tabs[0]
    tab.add_message(_tw_msg())
    tab.view._flush()
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    assert len(fired) == 1
    fired[0]()  # clock still at 0: BEFORE the 600 s deadline
    rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
    assert not any(m.flags & MessageFlags.DISABLED for m in rows)


async def test_timeout_expiry_skipped_when_newer_timeout_active(win, monkeypatch):
    """A re-timeout replaces the deadline — the old timer fires but must
    not un-gray the active entry's rows."""
    fired: list = []
    monkeypatch.setattr(
        "chattersyn.ui.main_window.QTimer.singleShot", lambda ms, cb: fired.append(cb))
    clock = {"now": 0.0}
    monkeypatch.setattr(
        "chattersyn.services.moderation.time.monotonic", lambda: clock["now"])
    tab = win._tabs[0]
    tab.add_message(_tw_msg())
    tab.view._flush()
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    clock["now"] = 60.0
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=1200))
    tab.view._flush()
    assert len(fired) == 2
    clock["now"] = 601
    fired[0]()  # the 600 s timer; its deadline was replaced by the 1200 s one
    rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
    assert all(m.flags & MessageFlags.DISABLED for m in rows)


async def test_untimeout_schedules_no_timer(win, monkeypatch):
    """ban-duration=0 (untimeout) must not arm an expiry timer."""
    fired: list = []
    monkeypatch.setattr(
        "chattersyn.ui.main_window.QTimer.singleShot", lambda ms, cb: fired.append(cb))
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    assert len(fired) == 1
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=0))
    assert len(fired) == 1


async def test_timeout_expiry_ungrays_when_entry_pruned_before_fire(win, monkeypatch):
    """If an is_disabled/disabled_logins call pruned the expired entry
    between timer arm and fire, the fire sees no entry — but nothing newer
    is active, so the rows must still un-gray (the deadline check alone
    would return early and leave them grayed forever)."""
    fired: list = []
    monkeypatch.setattr(
        "chattersyn.ui.main_window.QTimer.singleShot", lambda ms, cb: fired.append(cb))
    clock = {"now": 0.0}
    monkeypatch.setattr(
        "chattersyn.services.moderation.time.monotonic", lambda: clock["now"])
    tab = win._tabs[0]
    tab.add_message(_tw_msg())
    tab.view._flush()
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    assert len(fired) == 1
    clock["now"] = 700.0
    win._moderation.disabled_logins("forsen")  # prunes the expired entry
    fired[0]()
    rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
    assert not any(m.flags & MessageFlags.DISABLED for m in rows)


async def test_clearchat_full_clear_breaks_stack(win):
    """A full clear starts a new epoch: a later timeout for the same user
    must not fold onto the pre-clear row above the clear marker."""
    tab = win._tabs[0]
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    win._on_bridge_event(ClearChat(channel="forsen", user=None))
    win._on_bridge_event(ClearChat(channel="forsen", user="Bob", duration=600))
    tab.view._flush()
    assert [m.plain for m in _system_rows(win)] == [
        "Bob was timed out for 600s.",
        "Chat has been cleared by a moderator.",
        "Bob was timed out for 600s."]


# -- YouTube identity retry (verification pass: starvation on stable connections) -----


async def test_identity_retry_loops_until_resolved(win, monkeypatch):
    class FakeYt:
        tokens = "tok"

        def __init__(self):
            self.calls = 0

        def self_channel_id(self):
            return "UCme" if self.calls >= 2 else ""

        async def load_identity(self):
            self.calls += 1

    win._youtube = FakeYt()
    monkeypatch.setattr("chattersyn.ui.main_window.IDENTITY_RETRY_SECONDS", 0)
    await win._retry_identity()
    assert win._youtube.calls == 2  # first failed, second resolved


async def test_identity_retry_stops_when_signed_out(win):
    class FakeYt:
        tokens = None

        async def load_identity(self):
            raise AssertionError("must not run without tokens")

    win._youtube = FakeYt()
    await win._retry_identity()  # loop never entered


async def test_youtube_live_unknown_identity_schedules_retry(win, monkeypatch):
    """LIVE with identity unknown spawns ONE retry per 300 s and never
    while a retry loop is running."""
    captured: list = []
    state = {"done": False}

    def fake_ensure(coro):
        captured.append(coro)
        return types.SimpleNamespace(done=lambda: state["done"])

    monkeypatch.setattr(asyncio, "ensure_future", fake_ensure)
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "chattersyn.ui.main_window.time.monotonic", lambda: clock["now"])
    tab = win._tabs[0]
    win._youtube = types.SimpleNamespace(
        self_channel_id=lambda: "",
        owns_channel=lambda cid: False,
        tokens="tok")
    event = YoutubeState(
        watch_key=tab.tab_key, state=WatchState.LIVE,
        live_chat_id="lc1", channel_id="UCother", title="t")
    win._on_youtube_state(event)
    assert len(captured) == 1
    win._on_youtube_state(event)  # throttled by the 300 s window
    assert len(captured) == 1
    clock["now"] = 1500
    win._on_youtube_state(event)  # window open but a loop is in flight
    assert len(captured) == 1
    state["done"] = True
    win._on_youtube_state(event)
    assert len(captured) == 2


# -- YouTube lifecycle (verification pass: UNAVAILABLE orphaned ownership) -------------


async def test_youtube_unavailable_forgets_live_chat(win):
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    win._moderation.set_youtube_owner("lc1", True)
    win._moderation.remember_youtube_ban("lc1", "UCbad", "ban-1")
    win._update_moderation_available(tab)
    assert tab.header._shield.isEnabled()
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.UNAVAILABLE, detail="ended"))
    assert tab.youtube_live_chat_id == ""
    assert not tab.header._shield.isEnabled()
    assert not win._moderation.has_youtube_rights("lc1")
    assert win._moderation.youtube_ban_id("lc1", "UCbad") is None


async def test_youtube_not_found_forgets_stale_live_chat(win):
    """Hardening: a NOT_FOUND re-resolve that skipped ENDED (exception
    bypass in the watch loop) must still drop the dead chat id instead of
    keeping the shield on for a chat that no longer exists."""
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    win._moderation.set_youtube_owner("lc1", True)
    win._update_moderation_available(tab)
    assert tab.header._shield.isEnabled()
    win._on_youtube_state(YoutubeState(
        watch_key=tab.tab_key, state=WatchState.NOT_FOUND))
    assert tab.youtube_live_chat_id == ""
    assert not tab.header._shield.isEnabled()
    assert not win._moderation.has_youtube_rights("lc1")


# -- usercard without a channel id ------------------------------------------------------


async def test_usercard_youtube_delete_without_user_id_hides_by_login(win):
    """The synthesized /usercard card has no channel id (the chatter has no
    captured rows): /delete is a local hide that only needs the login."""
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    tab.add_message(Message.text(Platform.YOUTUBE, "lc1", Author("Bad", login="bad"), "spam"))
    tab.view._flush()
    win._on_usercard_action(
        tab, UsercardTarget(Platform.YOUTUBE, "bad", "Bad"), "/delete bad")
    tab.view._flush()
    rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
    assert rows and all(m.flags & MessageFlags.DISABLED for m in rows)
    assert "hid Bad" in _system_rows(win)[-1].plain


async def test_usercard_youtube_ban_without_user_id_skips(win):
    """Server-side actions need the channel id; without it the click
    produces the note and no API call."""
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    win._youtube = types.SimpleNamespace(api=_FakeYtApi())
    win._on_usercard_action(
        tab, UsercardTarget(Platform.YOUTUBE, "bad", "Bad"), "/ban bad")
    tab.view._flush()
    assert "no channel id recorded" in _system_rows(win)[-1].plain


async def test_usercard_delete_uses_rows_chat_id_after_clear(win):
    """A card opened from a row keeps its /delete working after the tab id
    is cleared (UNAVAILABLE/ENDED): it targets the chat the row came from,
    not the (now empty) tab id — 0 rows would silently hide nothing."""
    tab = win._tabs[0]
    tab.add_message(Message.text(
        Platform.YOUTUBE, "lc1", Author("Bad", login="bad"), "spam"))
    tab.view._flush()
    target = UsercardTarget(Platform.YOUTUBE, "bad", "Bad", channel_id="lc1")
    tab.youtube_live_chat_id = ""  # chat id cleared while the card was open
    win._on_usercard_action(tab, target, "/delete bad")
    tab.view._flush()
    rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
    assert rows and all(m.flags & MessageFlags.DISABLED for m in rows)
    assert "hid Bad" in _system_rows(win)[-1].plain


async def test_hide_button_uses_rows_chat_id_after_clear(win):
    """Per-row Hide targets the chat the row came from: rows survive a
    UNAVAILABLE/ENDED that clears the tab id, and the local hide must keep
    working (before, it matched 0 rows and claimed success anyway)."""
    tab = win._tabs[0]
    tab.youtube_live_chat_id = "lc1"
    author = Author("Bad", login="bad", user_id="UCbad")
    tab.add_message(Message.text(Platform.YOUTUBE, "lc1", author, "spam"))
    tab.view._flush()
    msg = Message.text(Platform.YOUTUBE, "lc1", author, "spam")
    tab.youtube_live_chat_id = ""
    win._on_youtube_mod_action(tab, msg, "Hide")
    tab.view._flush()
    rows = [m for m in tab.view.message_model.all_messages() if m.author is not None]
    assert len(rows) == 1 and all(m.flags & MessageFlags.DISABLED for m in rows)
    assert "hid Bad" in _system_rows(win)[-1].plain


# -- ROOMSTATE replay + auth status -----------------------------------------------------


async def test_roomstate_label_replayed_on_new_tab(win):
    win._on_bridge_event(RoomState(channel="forsen", state={"slow": 30, "subs_only": True}))
    assert not win._tabs[0].header._modes_label.isHidden()
    tab2 = win.add_tab("forsen", None)
    win._feeds[-1].stop()
    assert not tab2.header._modes_label.isHidden()
    assert "slow 30s" in tab2.header._modes_label.text()


async def test_roomstate_cache_dropped_when_last_tab_closes(win):
    """Closing the last tab for a channel drops the replay cache: modes
    may change while unjoined, and a refused re-join (90-channel cap)
    would otherwise replay the stale label forever."""
    win._on_bridge_event(RoomState(channel="forsen", state={"slow": 30}))
    assert win._roomstate_text.get("forsen")
    win._close_tab(0)
    assert "forsen" not in win._roomstate_text


async def test_twitch_auth_reconnect_failure_skips_connected_status(win, monkeypatch):
    """reconnect() False means the connection was NOT swapped — the status
    bar must not claim the account is connected."""
    class FakeTwitch:
        class Auth:
            async def begin_flow(self):
                return "http://example"

            async def wait_for_code(self):
                return object()

        def __init__(self):
            self.auth = self.Auth()

        async def reconnect(self):
            return False

    win._twitch = FakeTwitch()
    monkeypatch.setattr(
        "chattersyn.ui.main_window.QDesktopServices",
        types.SimpleNamespace(openUrl=lambda u: None))
    await win._run_twitch_auth()
    assert win.statusBar().currentMessage() != "Twitch account connected"


async def test_twitch_auth_reconnect_success_shows_connected_status(win, monkeypatch):
    class FakeTwitch:
        class Auth:
            async def begin_flow(self):
                return "http://example"

            async def wait_for_code(self):
                return object()

        def __init__(self):
            self.auth = self.Auth()

        async def reconnect(self):
            return True

    win._twitch = FakeTwitch()
    monkeypatch.setattr(
        "chattersyn.ui.main_window.QDesktopServices",
        types.SimpleNamespace(openUrl=lambda u: None))
    await win._run_twitch_auth()
    assert win.statusBar().currentMessage() == "Twitch account connected"


async def test_twitch_auth_failed_reconnect_never_claims_authenticated(win, monkeypatch):
    """The 'twitch authenticated' status must not appear while the token
    check inside reconnect() is still running — only a swapped connection
    is a connected account."""
    class FakeTwitch:
        class Auth:
            async def begin_flow(self):
                return "http://example"

            async def wait_for_code(self):
                return object()

        def __init__(self):
            self.auth = self.Auth()

        async def reconnect(self):
            return False

    win._twitch = FakeTwitch()
    monkeypatch.setattr(
        "chattersyn.ui.main_window.QDesktopServices",
        types.SimpleNamespace(openUrl=lambda u: None))
    await win._run_twitch_auth()
    assert win.statusBar().currentMessage() != "twitch authenticated"
