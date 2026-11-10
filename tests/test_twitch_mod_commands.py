"""run_helix_command: Helix calls and confirmation rows for moderation commands."""

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from chattersyn.services.twitch import auth
from chattersyn.services.twitch.helix import BASE, Helix, HelixError
from chattersyn.services.twitch.mod_commands import (
    COMMAND_SCOPES,
    HelixCommand,
    HelixResult,
    run_helix_command,
)

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)


async def _token():
    return "tok"


async def run(cmd, **kw):
    helix = Helix("cid", _token)
    try:
        return await run_helix_command(
            helix, cmd, channel="alice", broadcaster_id="10", moderator_id="99",
            now=kw.get("now", NOW))
    finally:
        await helix.close()


def body(route, i=0):
    return json.loads(route.calls[i].request.content)


def user_route(login="bob", uid="42"):
    return respx.get(f"{BASE}/users", params={"login": login}).mock(
        return_value=httpx.Response(200, json={"data": [{"id": uid, "login": login}]}))


@respx.mock
async def test_slow_sets_wait_time():
    route = respx.patch(f"{BASE}/chat/settings").mock(return_value=httpx.Response(200, json={}))
    res = await run(HelixCommand("slow", seconds=10))
    assert body(route) == {"slow_mode": True, "slow_mode_wait_time": 10}
    assert dict(route.calls[0].request.url.params) == {"broadcaster_id": "10", "moderator_id": "99"}
    assert res == HelixResult(row="Slow mode on (10 s).")


@respx.mock
async def test_slow_defaults_and_off():
    route = respx.patch(f"{BASE}/chat/settings").mock(return_value=httpx.Response(200, json={}))
    res = await run(HelixCommand("slow"))
    assert body(route) == {"slow_mode": True, "slow_mode_wait_time": 30}
    assert res.row == "Slow mode on (30 s)."
    res = await run(HelixCommand("slowoff"))
    assert body(route, 1) == {"slow_mode": False}
    assert res.row == "Slow mode off."


@respx.mock
async def test_commercial_default_30():
    route = respx.post(f"{BASE}/channels/commercial").mock(
        return_value=httpx.Response(200, json={}))
    res = await run(HelixCommand("commercial"))
    assert body(route) == {"broadcaster_id": "10", "length": 30}
    assert res.row == "Commercial started (30 s)."


@respx.mock
async def test_announce_blue():
    route = respx.post(f"{BASE}/chat/announcements").mock(
        return_value=httpx.Response(204))
    res = await run(HelixCommand("announceblue", text="Stream starts soon"))
    assert body(route) == {"message": "Stream starts soon", "color": "blue"}
    assert res.row == ""


@respx.mock
async def test_announce_has_no_row():
    route = respx.post(f"{BASE}/chat/announcements").mock(return_value=httpx.Response(204))
    res = await run(HelixCommand("announce", text="hello"))
    assert body(route)["color"] == "primary"
    assert res == HelixResult(row="")


@respx.mock
async def test_vip_resolves_user():
    user_route()
    route = respx.post(f"{BASE}/channels/vips").mock(return_value=httpx.Response(204))
    res = await run(HelixCommand("vip", user="bob"))
    assert dict(route.calls[0].request.url.params) == {"broadcaster_id": "10", "user_id": "42"}
    assert res.row == "You added bob as a VIP."


@respx.mock
async def test_user_rows():
    user_route()
    respx.post(f"{BASE}/moderation/moderators").mock(return_value=httpx.Response(204))
    respx.delete(f"{BASE}/moderation/moderators").mock(return_value=httpx.Response(204))
    respx.delete(f"{BASE}/channels/vips").mock(return_value=httpx.Response(204))
    respx.post(f"{BASE}/moderation/warnings").mock(return_value=httpx.Response(200, json={}))
    assert (await run(HelixCommand("mod", user="bob"))).row == "You added bob as a moderator."
    assert (await run(HelixCommand("unmod", user="bob"))).row == "You removed bob as a moderator."
    assert (await run(HelixCommand("unvip", user="bob"))).row == "You removed bob as a VIP."
    assert (await run(HelixCommand("warn", user="bob", text="be nice"))).row == "You warned bob."


@respx.mock
async def test_warn_sends_reason():
    user_route()
    route = respx.post(f"{BASE}/moderation/warnings").mock(
        return_value=httpx.Response(200, json={}))
    await run(HelixCommand("warn", user="bob", text="be nice"))
    assert body(route) == {"data": {"user_id": "42", "reason": "be nice"}}


@respx.mock
async def test_followers_zero_any_follower():
    route = respx.patch(f"{BASE}/chat/settings").mock(return_value=httpx.Response(200, json={}))
    res = await run(HelixCommand("followers"))
    assert body(route) == {"follower_mode": True, "follower_mode_duration": 0}
    assert res.row == "Followers-only mode on (any follower)."


@respx.mock
async def test_followers_sends_minutes():
    route = respx.patch(f"{BASE}/chat/settings").mock(return_value=httpx.Response(200, json={}))
    res = await run(HelixCommand("followers", seconds=600))
    assert body(route) == {"follower_mode": True, "follower_mode_duration": 10}
    assert res.row == "Followers-only mode on (10 min)."
    res = await run(HelixCommand("followersoff"))
    assert body(route, 1) == {"follower_mode": False}
    assert res.row == "Followers-only mode off."


@respx.mock
@pytest.mark.parametrize("name,field,label", [
    ("emoteonly", "emote_mode", "Emote-only"),
    ("subscribers", "subscriber_mode", "Subscribers-only"),
    ("uniquechat", "unique_chat_mode", "Unique-chat"),
    ("r9kbeta", "unique_chat_mode", "Unique-chat"),
])
async def test_toggle_modes(name, field, label):
    route = respx.patch(f"{BASE}/chat/settings").mock(return_value=httpx.Response(200, json={}))
    on = await run(HelixCommand(name))
    off = await run(HelixCommand(name + "off"))
    assert body(route, 0) == {field: True}
    assert body(route, 1) == {field: False}
    assert on.row == f"{label} mode on."
    assert off.row == f"{label} mode off."


@respx.mock
async def test_shield_raid_shoutout_marker():
    shield = respx.put(f"{BASE}/moderation/shield_mode").mock(
        return_value=httpx.Response(200, json={}))
    user_route("carol", "77")
    raid = respx.post(f"{BASE}/raids").mock(return_value=httpx.Response(200, json={}))
    unraid = respx.delete(f"{BASE}/raids").mock(return_value=httpx.Response(204))
    shout = respx.post(f"{BASE}/chat/shoutouts").mock(return_value=httpx.Response(204))
    marker = respx.post(f"{BASE}/streams/markers").mock(return_value=httpx.Response(200, json={}))
    assert (await run(HelixCommand("shield"))).row == "Shield mode on."
    assert (await run(HelixCommand("shieldoff"))).row == "Shield mode off."
    assert body(shield, 0) == {"is_active": True}
    assert body(shield, 1) == {"is_active": False}
    assert (await run(HelixCommand("raid", user="carol"))).row == "Raiding carol."
    assert dict(raid.calls[0].request.url.params) == {
        "from_broadcaster_id": "10", "to_broadcaster_id": "77"}
    assert (await run(HelixCommand("unraid"))).row == "Raid cancelled."
    assert unraid.call_count == 1
    assert (await run(HelixCommand("shoutout", user="carol"))).row == "You gave carol a shoutout."
    assert dict(shout.calls[0].request.url.params)["to_broadcaster_id"] == "77"
    assert (await run(HelixCommand("marker"))).row == "Stream marker added."
    assert body(marker) == {"user_id": "10"}


@respx.mock
async def test_clear_has_no_row():
    route = respx.delete(f"{BASE}/moderation/chat").mock(return_value=httpx.Response(204))
    res = await run(HelixCommand("clear"))
    assert res == HelixResult(row="")
    assert "message_id" not in route.calls[0].request.url.params


@respx.mock
async def test_delete_message_id():
    route = respx.delete(f"{BASE}/moderation/chat").mock(return_value=httpx.Response(204))
    res = await run(HelixCommand("delete", message_id="abc-1"))
    assert route.calls[0].request.url.params["message_id"] == "abc-1"
    assert res.row == ""


@respx.mock
async def test_settitle():
    route = respx.patch(f"{BASE}/channels").mock(return_value=httpx.Response(204))
    res = await run(HelixCommand("settitle", text="Hello world"))
    assert body(route) == {"title": "Hello world"}
    assert res.row == "Title set to Hello world."


def _search(*names):
    return respx.get(f"{BASE}/search/categories").mock(return_value=httpx.Response(
        200, json={"data": [{"id": str(i), "name": n} for i, n in enumerate(names, 1)]}))


@respx.mock
async def test_setgame_top_search_hit():
    _search("Grand Theft Auto V", "Grand Theft Auto IV")
    route = respx.patch(f"{BASE}/channels").mock(return_value=httpx.Response(204))
    res = await run(HelixCommand("setgame", text="gta 5"))
    assert body(route) == {"game_id": "1"}
    assert res.row == "Category set to Grand Theft Auto V."


@respx.mock
async def test_setgame_exact_match_ignores_case():
    _search("Just Chatting Sims", "Just Chatting")
    route = respx.patch(f"{BASE}/channels").mock(return_value=httpx.Response(204))
    res = await run(HelixCommand("setgame", text="just chatting"))
    assert body(route) == {"game_id": "2"}
    assert res.row == "Category set to Just Chatting."


@respx.mock
async def test_setgame_no_match_changes_nothing():
    _search()
    route = respx.patch(f"{BASE}/channels").mock(return_value=httpx.Response(204))
    res = await run(HelixCommand("setgame", text="zzz"))
    assert route.call_count == 0
    assert res == HelixResult(row="No Twitch category matches zzz.")


@respx.mock
async def test_uptime_live_and_offline():
    live = respx.get(f"{BASE}/streams").mock(return_value=httpx.Response(200, json={"data": [{
        "title": "t", "viewer_count": 1, "started_at": "2026-11-10T09:46:00Z"}]}))
    res = await run(HelixCommand("uptime"))
    assert res.row == "alice has been live for 2h 14m"
    live.mock(return_value=httpx.Response(200, json={"data": [{
        "title": "t", "viewer_count": 1, "started_at": "2026-11-10T11:46:00Z"}]}))
    assert (await run(HelixCommand("uptime"))).row == "alice has been live for 14m"
    live.mock(return_value=httpx.Response(200, json={"data": []}))
    assert (await run(HelixCommand("uptime"))).row == "alice is offline"


@respx.mock
async def test_mods_sorted_names():
    respx.get(f"{BASE}/moderation/moderators").mock(return_value=httpx.Response(
        200, json={"data": [{"user_login": "zed"}, {"user_login": "Bob"}, {"user_login": "amy"}]}))
    res = await run(HelixCommand("mods"))
    assert res == HelixResult(row="", names=["amy", "Bob", "zed"])
    respx.get(f"{BASE}/channels/vips").mock(return_value=httpx.Response(
        200, json={"data": [{"user_login": "v2"}, {"user_login": "v1"}]}))
    assert (await run(HelixCommand("vips"))).names == ["v1", "v2"]


@respx.mock
async def test_unknown_user_raises():
    respx.get(f"{BASE}/users").mock(return_value=httpx.Response(200, json={"data": []}))
    post = respx.post(f"{BASE}/channels/vips").mock(return_value=httpx.Response(204))
    with pytest.raises(HelixError, match="No Twitch user named ghost"):
        await run(HelixCommand("vip", user="ghost"))
    assert post.call_count == 0


@respx.mock
async def test_twitch_refusal_raises_helix_error():
    user_route()
    respx.post(f"{BASE}/channels/vips").mock(return_value=httpx.Response(
        422, json={"message": "already a VIP"}))
    with pytest.raises(HelixError):
        await run(HelixCommand("vip", user="bob"))


def test_every_command_has_a_scope_entry():
    expected = {
        "warn": auth.WARNINGS_SCOPE,
        "announce": auth.ANNOUNCEMENTS_SCOPE, "announceblue": auth.ANNOUNCEMENTS_SCOPE,
        "announcegreen": auth.ANNOUNCEMENTS_SCOPE, "announceorange": auth.ANNOUNCEMENTS_SCOPE,
        "announcepurple": auth.ANNOUNCEMENTS_SCOPE,
        "shoutout": auth.SHOUTOUTS_SCOPE,
        "shield": auth.SHIELD_SCOPE, "shieldoff": auth.SHIELD_SCOPE,
        "clear": auth.CHAT_MESSAGES_SCOPE, "delete": auth.CHAT_MESSAGES_SCOPE,
        "mod": auth.MODERATORS_SCOPE, "unmod": auth.MODERATORS_SCOPE,
        "mods": auth.MODERATORS_SCOPE,
        "vip": auth.VIPS_SCOPE, "unvip": auth.VIPS_SCOPE, "vips": auth.VIPS_SCOPE,
        "raid": auth.RAIDS_SCOPE, "unraid": auth.RAIDS_SCOPE,
        "commercial": auth.COMMERCIAL_SCOPE,
        "marker": auth.BROADCAST_SCOPE, "settitle": auth.BROADCAST_SCOPE,
        "setgame": auth.BROADCAST_SCOPE,
        "uptime": None,
    }
    for name in ("slow", "slowoff", "followers", "followersoff", "emoteonly", "emoteonlyoff",
                 "subscribers", "subscribersoff", "uniquechat", "uniquechatoff",
                 "r9kbeta", "r9kbetaoff"):
        expected[name] = auth.CHAT_SETTINGS_SCOPE
    assert expected == COMMAND_SCOPES
