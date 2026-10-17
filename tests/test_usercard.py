"""UsercardPopup: rows, recent-message filter, moderation buttons, profile link."""

import datetime as dt

import pytest
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel

from chattersyn.config.settings import Settings
from chattersyn.models.message import Author, Message, Platform
from chattersyn.ui.usercard import (
    UsercardPopup,
    UsercardTarget,
    _duration_text,
    recent_messages_for,
)


def _msg(text, msg_id, name="Bob", login="bob", platform=Platform.TWITCH,
         system=False):
    if system:
        return Message.system(platform, "forsen", text, id=msg_id)
    return Message(
        id=msg_id, platform=platform, channel_id="forsen",
        ts=dt.datetime(2026, 10, 16, 9, 5, 7),
        author=Author(name=name, login=login), plain=text,
    )


def _target(**overrides):
    kw = dict(platform=Platform.TWITCH, login="bob", display_name="Bob",
              user_id="123", avatar_url=None, color="#ff0000", is_self=False)
    kw.update(overrides)
    return UsercardTarget(**kw)


# -- recent_messages_for ------------------------------------------------------------------


def test_recent_matches_login_case_insensitive():
    msgs = [
        _msg("hi", "m1", login="Bob"),
        _msg("hello", "m2", login="bob2"),
        _msg("system", "m3", system=True),
        _msg("bob subscribed at tier 1!", "m4", system=True),
    ]
    found = [m.id for m in recent_messages_for(msgs, "BOB")]
    assert found == ["m1", "m4"]  # own messages + system rows naming them


def test_recent_excludes_other_users_and_plain_systems():
    msgs = [_msg("hi", "m1", login="alice"), _msg("system", "m2", system=True)]
    assert recent_messages_for(msgs, "bob") == []


# -- duration text ------------------------------------------------------------------------


def test_duration_text_units():
    now = dt.datetime.now(dt.UTC)
    assert _duration_text((now - dt.timedelta(days=10)).isoformat()) == "10 days"
    assert _duration_text((now - dt.timedelta(days=100)).isoformat()) == "3 months"
    assert _duration_text((now - dt.timedelta(days=800)).isoformat()) == "2 years"
    assert _duration_text("garbage") == ""


# -- rows ----------------------------------------------------------------------------------


@pytest.fixture
def popup(qapp):
    return UsercardPopup(Settings({}), _target(), [])


def test_identity_and_id_rows(popup):
    assert popup._name_label.text() == "Bob"
    assert popup._login_label.text() == "@bob"
    assert popup._id_value.text() == "123"
    assert not popup._id_row.isHidden()


def test_detail_rows_hidden_until_enriched(popup):
    assert popup._created_row.isHidden()
    assert popup._followers_row.isHidden()
    assert popup._followage_row.isHidden()


def test_set_details_fills_rows(popup):
    popup.set_details(created_at="2021-03-04T05:06:07Z", followers=12345,
                      followage="2022-01-01T00:00:00Z")
    assert not popup._created_row.isHidden()
    assert popup._created_value.text() == "2021-03-04"
    assert popup._created_value.toolTip().startswith("Channel created")
    assert not popup._followers_row.isHidden()
    assert popup._followers_value.text() == "12,345"
    assert not popup._followage_row.isHidden()
    assert popup._followage_value.text() == "2022-01-01"


def test_youtube_uses_subscribers_label(qapp):
    popup = UsercardPopup(Settings({}), _target(platform=Platform.YOUTUBE), [])
    labels = [w.text() for w in popup._followers_row.findChildren(QLabel)]
    assert "Subscribers:" in labels


def test_twitch_uses_followers_label(qapp):
    popup = UsercardPopup(Settings({}), _target(platform=Platform.TWITCH), [])
    labels = [w.text() for w in popup._followers_row.findChildren(QLabel)]
    assert "Followers:" in labels


# -- avatar / profile link -------------------------------------------------------------------


def test_avatar_opens_twitch_profile(popup, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    popup._avatar.click()
    assert opened == ["https://www.twitch.tv/bob"]


def test_avatar_opens_youtube_handle(qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    popup = UsercardPopup(Settings({}), _target(platform=Platform.YOUTUBE), [])
    popup._avatar.click()
    assert opened == ["https://www.youtube.com/@bob"]


# -- moderation buttons -----------------------------------------------------------------------


def test_mod_section_hidden_without_mod_rights(popup):
    assert popup._mod_frame.isHidden()


def test_mod_section_visible_with_mod_rights(qapp):
    popup = UsercardPopup(Settings({}), _target(), [], self_is_mod=True)
    assert not popup._mod_frame.isHidden()


def test_mod_section_hidden_for_self_even_as_mod(qapp):
    popup = UsercardPopup(Settings({}), _target(is_self=True), [], self_is_mod=True)
    assert popup._mod_frame.isHidden()


def test_mod_buttons_emit_command_text(qapp):
    popup = UsercardPopup(Settings({}), _target(), [], self_is_mod=True)
    got = []
    popup.action_requested.connect(got.append)
    labels = {button.text(): button for button in popup._mod_buttons}
    labels["Ban"].click()
    assert got[-1] == "/ban bob"
    labels["30s"].click()
    assert got[-1] == "/timeout bob 30s"
    labels["1w"].click()
    assert got[-1] == "/timeout bob 1w"
    labels["Unban"].click()
    assert got[-1] == "/unban bob"
    assert set(labels) >= {"Ban", "Unban", "1s", "30s", "1m", "5m", "30m", "1h", "1d", "1w"}


def test_youtube_mod_strip_is_fixed_set(qapp):
    popup = UsercardPopup(Settings({}), _target(platform=Platform.YOUTUBE), [],
                          self_is_mod=True)
    labels = {button.text() for button in popup._mod_buttons}
    # YouTube: server ban + local delete + fixed 300s timeout (docs/m4.md).
    assert labels == {"Ban", "Unban", "Timeout 5m", "Delete"}


def test_custom_mod_actions_expand_user_placeholders(qapp):
    popup = UsercardPopup(
        Settings({}), _target(), [], self_is_mod=True,
        custom_actions=[{"name": "Permaban", "template": "/ban {user.name}"},
                        {"name": "Shout", "template": "/shoutout {user.login}"}])
    got = []
    popup.action_requested.connect(got.append)
    labels = {button.text(): button for button in popup._mod_buttons}
    labels["Permaban"].click()
    assert got[-1] == "/ban Bob"
    labels["Shout"].click()
    assert got[-1] == "/shoutout bob"


def test_invalid_custom_actions_skipped(qapp):
    popup = UsercardPopup(Settings({}), _target(), [], self_is_mod=True,
                          custom_actions=[{"template": "/ban {user.name}"}, "junk"])
    labels = {button.text() for button in popup._mod_buttons}
    assert "junk" not in labels
    assert len(labels) == 10  # Ban + Unban + strip


# -- recent messages -------------------------------------------------------------------------


def test_recent_initial_and_count(qapp):
    entries = [("tag", _msg("hi", "m1")), ("tag", _msg("there", "m2"))]
    popup = UsercardPopup(Settings({}), _target(), entries)
    assert popup._recent.model().rowCount() == 2
    assert popup._recent_label.text() == "Recent messages (2)"


def test_recent_empty_state(popup):
    assert popup._recent.model().rowCount() == 0
    assert popup._recent_label.text() == "No recent messages"


def test_append_recent_grows_live(qapp):
    popup = UsercardPopup(Settings({}), _target(), [])
    popup.append_recent(("tag", _msg("fresh", "m9")))
    assert popup._recent.model().rowCount() == 1
    assert popup._recent_label.text() == "Recent messages (1)"
