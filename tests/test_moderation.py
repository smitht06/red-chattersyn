"""ModerationState: mod-rights bookkeeping, ClearChat stacking, YouTube bans."""

from chattersyn.models.message import Badge
from chattersyn.services.moderation import MOD_BADGE_SETS, ModerationState


def test_twitch_rights_via_userstate_mod():
    s = ModerationState()
    assert not s.has_twitch_rights("forsen")
    s.set_twitch_mod("#forsen", True)
    assert s.has_twitch_rights("forsen")
    s.set_twitch_mod("forsen", False)
    assert not s.has_twitch_rights("forsen")


def test_twitch_rights_via_self_badges():
    s = ModerationState()
    s.note_twitch_self_badges("forsen", [Badge("moderator", "1")])
    assert s.has_twitch_rights("forsen")
    s.note_twitch_self_badges("forsen", [Badge("vip", "1")])
    assert not s.has_twitch_rights("forsen")


def test_twitch_rights_as_broadcaster_by_name():
    s = ModerationState()
    s.set_self_identity("Forsen")
    assert s.has_twitch_rights("forsen")
    s.set_self_identity("someoneelse")
    assert not s.has_twitch_rights("forsen")


def test_clearchat_stack_counts_and_clears():
    s = ModerationState()
    assert s.note_clearchat("forsen", "Bob", 600) == 1
    assert s.last_clearchat("forsen", "bob") is not None
    assert s.note_clearchat("forsen", "bob", 600) == 2
    s.clear_stack("forsen", "bob")
    assert s.last_clearchat("forsen", "bob") is None
    assert s.note_clearchat("forsen", "bob", None) == 1  # permanent ban restarts


def test_disabled_logins_expire(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr("chattersyn.services.moderation.time.monotonic", lambda: clock["t"])
    s = ModerationState()
    s.note_clearchat("forsen", "Bob", 600)
    assert s.is_disabled("forsen", "bob")
    assert s.disabled_logins("forsen") == {"bob"}
    clock["t"] = 700.1
    assert not s.is_disabled("forsen", "bob")


def test_permanent_ban_never_expires(monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr("chattersyn.services.moderation.time.monotonic", lambda: clock["t"])
    s = ModerationState()
    s.note_clearchat("forsen", "Bob", None)
    clock["t"] = 1e9
    assert s.is_disabled("forsen", "bob")


def test_clearchat_zero_duration_expires_immediately(monkeypatch):
    """Duration 0 must not act like a permanent ban: the deadline is 'now',
    so the entry expires as soon as time advances. (MainWindow routes
    untimeouts through clear_disabled; this guards the bookkeeping path.)"""
    clock = {"t": 100.0}
    monkeypatch.setattr("chattersyn.services.moderation.time.monotonic", lambda: clock["t"])
    s = ModerationState()
    s.note_clearchat("forsen", "Bob", 0)
    assert s.is_disabled("forsen", "bob")  # deadline == now: still active
    clock["t"] = 100.0001
    assert not s.is_disabled("forsen", "bob")


def test_clear_disabled_pops_entry():
    s = ModerationState()
    s.note_clearchat("forsen", "Bob", None)
    assert s.is_disabled("forsen", "bob")
    s.clear_disabled("forsen", "BOB")
    assert not s.is_disabled("forsen", "bob")


def test_disabled_until_accessor(monkeypatch):
    """The armed-deadline accessor used by the expiry timer: pure read,
    no expiry pruning."""
    clock = {"t": 100.0}
    monkeypatch.setattr("chattersyn.services.moderation.time.monotonic", lambda: clock["t"])
    s = ModerationState()
    assert s.disabled_until("forsen", "bob") is None
    s.note_clearchat("forsen", "Bob", 600)
    assert s.disabled_until("forsen", "bob") == 700.0
    s.note_clearchat("forsen", "Bob", None)
    assert s.disabled_until("forsen", "bob") is None  # permanent: no deadline
    s.clear_disabled("forsen", "bob")
    assert s.disabled_until("forsen", "bob") is None


def test_clear_stacks_for_channel_keeps_bans():
    """A full clear breaks every stack; active timeouts/bans stay registered."""
    s = ModerationState()
    s.note_clearchat("forsen", "Bob", 600)
    s.note_clearchat("forsen", "Alice", None)
    s.clear_stacks_for_channel("forsen")
    assert s.last_clearchat("forsen", "bob") is None
    assert s.last_clearchat("forsen", "alice") is None
    assert s.is_disabled("forsen", "bob")
    assert s.is_disabled("forsen", "alice")
    assert s.note_clearchat("forsen", "bob", 600) == 1  # a fresh stack


def test_mod_badge_sets_match_spec():
    """docs/m4.md: moderator/lead_moderator/mod=1 carry rights; twitchbot is
    a bot badge, not a mod right."""
    assert {"moderator", "lead_moderator", "staff", "admin", "global_mod"} <= MOD_BADGE_SETS
    assert "twitchbot" not in MOD_BADGE_SETS


def test_youtube_owner_gating():
    s = ModerationState()
    assert not s.youtube_owner_known("lc1")
    assert not s.has_youtube_rights("lc1")
    s.set_youtube_owner("lc1", True)
    assert s.youtube_owner_known("lc1") and s.has_youtube_rights("lc1")
    s.set_youtube_owner("lc1", False)
    assert s.youtube_owner_known("lc1") and not s.has_youtube_rights("lc1")


def test_youtube_ban_id_memory():
    s = ModerationState()
    assert s.youtube_ban_id("lc1", "UCx") is None
    s.remember_youtube_ban("lc1", "UCx", "ban-9")
    assert s.youtube_ban_id("lc1", "UCx") == "ban-9"
    s.clear_youtube_ban("lc1", "UCx")
    assert s.youtube_ban_id("lc1", "UCx") is None
