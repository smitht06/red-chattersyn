"""Helix client: user-token provider, 401 repull, badges, emotes, users."""

import httpx
import respx

from chattersyn.models.message import Badge
from chattersyn.services.twitch.helix import BASE, Helix, HelixError


class FakeTokenProvider:
    """Returns the queued tokens one by one, then None; counts pulls."""

    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return self.tokens.pop(0) if self.tokens else None


def make_helix(tokens):
    provider = FakeTokenProvider(tokens)
    return provider, Helix("cid", provider)


@respx.mock
async def test_token_pulled_once_and_reused():
    provider, helix = make_helix(["tok-1"])
    api_route = respx.get(f"{BASE}/chat/badges/global").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    try:
        await helix.global_badges()
        await helix.global_badges()
    finally:
        await helix.close()
    assert provider.calls == 1
    assert api_route.call_count == 2
    assert api_route.calls[0].request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
async def test_401_forces_repull_then_retry():
    provider, helix = make_helix(["tok-stale", "tok-new"])
    api_route = respx.get(f"{BASE}/chat/badges/global").mock(
        side_effect=[
            httpx.Response(401, json={"error": "Unauthorized"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    try:
        await helix.global_badges()
    finally:
        await helix.close()
    assert provider.calls == 2
    assert api_route.call_count == 2
    assert api_route.calls[1].request.headers["Authorization"] == "Bearer tok-new"


@respx.mock
async def test_401_with_no_replacement_token_raises():
    provider, helix = make_helix(["tok-stale"])
    api_route = respx.get(f"{BASE}/chat/badges/global").mock(
        return_value=httpx.Response(401, json={"error": "Unauthorized"})
    )
    try:
        try:
            await helix.global_badges()
            raise AssertionError("expected HelixError")
        except HelixError:
            pass
    finally:
        await helix.close()
    assert provider.calls == 2
    assert api_route.call_count == 1  # no second attempt without a token


@respx.mock
async def test_no_token_raises_without_api_call():
    provider, helix = make_helix([])
    api_route = respx.get(f"{BASE}/chat/badges/global").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    try:
        try:
            await helix.global_badges()
            raise AssertionError("expected HelixError")
        except HelixError:
            pass
    finally:
        await helix.close()
    assert provider.calls == 1
    assert api_route.call_count == 0


@respx.mock
async def test_second_401_propagates_http_error():
    provider, helix = make_helix(["tok-stale", "tok-new"])
    respx.get(f"{BASE}/chat/badges/global").mock(
        return_value=httpx.Response(401, json={"error": "Unauthorized"})
    )
    try:
        try:
            await helix.global_badges()
            raise AssertionError("expected HTTPStatusError")
        except httpx.HTTPStatusError:
            pass
    finally:
        await helix.close()
    assert provider.calls == 2


@respx.mock
async def test_invalidate_token_forces_repull():
    provider, helix = make_helix(["tok-1", "tok-2"])
    respx.get(f"{BASE}/chat/badges/global").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    try:
        await helix.global_badges()
        helix.invalidate_token()
        await helix.global_badges()
    finally:
        await helix.close()
    assert provider.calls == 2


@respx.mock
async def test_badges_parsed_into_lookup():
    respx.get(f"{BASE}/chat/badges/global").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "set_id": "moderator",
                        "versions": [
                            {
                                "id": "1",
                                "image_url_1x": "https://x/m1.png",
                                "image_url_2x": "https://x/m2.png",
                                "image_url_4x": "https://x/m4.png",
                                "title": "Moderator",
                                "click_action": None,
                                "description": "Chat moderator",
                            }
                        ],
                    }
                ]
            },
        )
    )
    _, helix = make_helix(["tok-1"])
    try:
        badges = await helix.global_badges()
    finally:
        await helix.close()
    assert isinstance(badges["moderator"]["1"], Badge)
    assert badges["moderator"]["1"].title == "Moderator"
    assert badges["moderator"]["1"].urls["4x"] == "https://x/m4.png"


@respx.mock
async def test_channel_emotes_and_user():
    respx.get(f"{BASE}/chat/emotes", params={"broadcaster_id": "1"}).mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "25", "name": "Kappa"}, {"id": "1902", "name": "Keepo"}]}
        )
    )
    respx.get(f"{BASE}/users", params={"login": "forsen"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "22484632",
                        "login": "forsen",
                        "display_name": "Forsen",
                        "profile_image_url": "https://x/p.png",
                        "created_at": "2013-11-25T00:00:00Z",
                    }
                ]
            },
        )
    )
    respx.get(f"{BASE}/users", params={"login": "nobody_zzz"}).mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    _, helix = make_helix(["tok-1"])
    try:
        assert await helix.channel_emotes("1") == {"25": "Kappa", "1902": "Keepo"}
        user = await helix.user_by_login("forsen")
        assert user and user["id"] == "22484632" and user["display_name"] == "Forsen"
        assert user["created_at"] == "2013-11-25T00:00:00Z"  # usercard Created row
        assert await helix.user_by_login("nobody_zzz") is None
    finally:
        await helix.close()


@respx.mock
async def test_global_emotes():
    respx.get(f"{BASE}/chat/emotes/global").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "1", "name": "BibleThump"}, {"id": "25", "name": "Kappa"}]}
        )
    )
    _, helix = make_helix(["tok-1"])
    try:
        assert await helix.global_emotes() == {"1": "BibleThump", "25": "Kappa"}
    finally:
        await helix.close()


@respx.mock
async def test_channel_follower_since():
    """TUF-4/5: /channels/followers?user_id= gives when a user followed a channel."""
    respx.get(f"{BASE}/channels/followers", params={"broadcaster_id": "2", "user_id": "1"}).mock(
        return_value=httpx.Response(200, json={"total": 50, "data": [
            {"user_id": "1", "user_login": "bob", "followed_at": "2024-03-05T10:00:00Z"}]}))
    respx.get(f"{BASE}/channels/followers", params={"broadcaster_id": "2", "user_id": "9"}).mock(
        return_value=httpx.Response(200, json={"total": 50, "data": []}))
    _, helix = make_helix(["tok-1"])
    try:
        assert await helix.channel_follower_since("2", "1") == "2024-03-05T10:00:00Z"
        assert await helix.channel_follower_since("2", "9") is None
    finally:
        await helix.close()


@respx.mock
async def test_channel_followers_total():
    respx.get(f"{BASE}/channels/followers", params={"broadcaster_id": "2"}).mock(
        return_value=httpx.Response(200, json={"total": 12345})
    )
    respx.get(f"{BASE}/channels/followers", params={"broadcaster_id": "9"}).mock(
        return_value=httpx.Response(200, json={"total": "not-a-number"})
    )
    _, helix = make_helix(["tok-1"])
    try:
        assert await helix.channel_followers("2") == 12345
        assert await helix.channel_followers("9") is None
    finally:
        await helix.close()



# -- moderation -----------------------------------------------------------------------


@respx.mock
async def test_ban_and_timeout_post_moderation_bans():
    import json
    route = respx.post(f"{BASE}/moderation/bans").mock(
        return_value=httpx.Response(200, json={"data": [{}]}))
    _, helix = make_helix(["tok-1"])
    try:
        await helix.ban_user("10", "20", "30", reason="spam")
        await helix.ban_user("10", "20", "30", duration=600)
    finally:
        await helix.close()
    first, second = (call.request for call in route.calls)
    assert dict(first.url.params) == {"broadcaster_id": "10", "moderator_id": "20"}
    assert json.loads(first.content) == {"data": {"user_id": "30", "reason": "spam"}}
    assert json.loads(second.content) == {"data": {"user_id": "30", "duration": 600}}


@respx.mock
async def test_unban_deletes_moderation_bans():
    route = respx.delete(f"{BASE}/moderation/bans").mock(return_value=httpx.Response(204))
    _, helix = make_helix(["tok-1"])
    try:
        await helix.unban_user("10", "20", "30")
    finally:
        await helix.close()
    assert dict(route.calls[0].request.url.params) == {
        "broadcaster_id": "10", "moderator_id": "20", "user_id": "30"}


@respx.mock
async def test_moderation_error_carries_twitch_message():
    respx.post(f"{BASE}/moderation/bans").mock(return_value=httpx.Response(
        400, json={"error": "Bad Request", "status": 400,
                   "message": "The user specified in the user_id field is already banned."}))
    _, helix = make_helix(["tok-1"])
    try:
        await helix.ban_user("10", "20", "30")
        raise AssertionError("expected HelixError")
    except HelixError as exc:
        assert "already banned" in str(exc)
    finally:
        await helix.close()


# -- moderation / broadcaster command endpoints (THM-7) ---------------------------------

import json  # noqa: E402

import pytest  # noqa: E402


async def _call_once(method, path, response, invoke):
    route = respx.route(method=method, url=f"{BASE}{path}").mock(return_value=response)
    _, helix = make_helix(["tok-1"])
    try:
        result = await invoke(helix)
    finally:
        await helix.close()
    request = route.calls[0].request
    body = json.loads(request.content) if request.content else None
    return result, dict(request.url.params), body


OK = httpx.Response(200, json={"data": [{}]})
NO_CONTENT = httpx.Response(204)


@respx.mock
async def test_warn_user_posts_moderation_warnings():
    _, params, body = await _call_once(
        "POST", "/moderation/warnings", OK, lambda h: h.warn_user("10", "20", "30", "be nice"))
    assert params == {"broadcaster_id": "10", "moderator_id": "20"}
    assert body == {"data": {"user_id": "30", "reason": "be nice"}}


@respx.mock
@pytest.mark.parametrize("method_name,http,path", [
    ("add_moderator", "POST", "/moderation/moderators"),
    ("remove_moderator", "DELETE", "/moderation/moderators"),
    ("add_vip", "POST", "/channels/vips"),
    ("remove_vip", "DELETE", "/channels/vips"),
])
async def test_role_endpoints(method_name, http, path):
    _, params, body = await _call_once(
        http, path, NO_CONTENT, lambda h: getattr(h, method_name)("10", "30"))
    assert params == {"broadcaster_id": "10", "user_id": "30"}
    assert body is None


@respx.mock
async def test_send_announcement_posts_message_and_color():
    _, params, body = await _call_once(
        "POST", "/chat/announcements", NO_CONTENT,
        lambda h: h.send_announcement("10", "20", "hello", "green"))
    assert params == {"broadcaster_id": "10", "moderator_id": "20"}
    assert body == {"message": "hello", "color": "green"}


@respx.mock
async def test_chat_settings_sends_only_given_fields():
    _, params, body = await _call_once(
        "PATCH", "/chat/settings", OK,
        lambda h: h.update_chat_settings("10", "20", slow_mode=True, slow_mode_wait_time=10))
    assert params == {"broadcaster_id": "10", "moderator_id": "20"}
    assert body == {"slow_mode": True, "slow_mode_wait_time": 10}


@respx.mock
async def test_send_shoutout_posts_ids_as_query():
    _, params, body = await _call_once(
        "POST", "/chat/shoutouts", NO_CONTENT, lambda h: h.send_shoutout("10", "20", "40"))
    assert params == {"from_broadcaster_id": "10", "to_broadcaster_id": "40",
                      "moderator_id": "20"}
    assert body is None


@respx.mock
async def test_set_shield_mode_puts_is_active():
    _, params, body = await _call_once(
        "PUT", "/moderation/shield_mode", OK, lambda h: h.set_shield_mode("10", "20", True))
    assert params == {"broadcaster_id": "10", "moderator_id": "20"}
    assert body == {"is_active": True}


@respx.mock
async def test_delete_chat_messages_with_and_without_id():
    _, params, _ = await _call_once(
        "DELETE", "/moderation/chat", NO_CONTENT,
        lambda h: h.delete_chat_messages("10", "20", "abc-123"))
    assert params == {"broadcaster_id": "10", "moderator_id": "20", "message_id": "abc-123"}
    respx.reset()
    _, params, _ = await _call_once(
        "DELETE", "/moderation/chat", NO_CONTENT, lambda h: h.delete_chat_messages("10", "20"))
    assert params == {"broadcaster_id": "10", "moderator_id": "20"}


@respx.mock
async def test_start_and_cancel_raid():
    _, params, _ = await _call_once("POST", "/raids", OK, lambda h: h.start_raid("10", "40"))
    assert params == {"from_broadcaster_id": "10", "to_broadcaster_id": "40"}
    respx.reset()
    _, params, _ = await _call_once("DELETE", "/raids", NO_CONTENT, lambda h: h.cancel_raid("10"))
    assert params == {"broadcaster_id": "10"}


@respx.mock
async def test_start_commercial_posts_length():
    _, _, body = await _call_once(
        "POST", "/channels/commercial", OK, lambda h: h.start_commercial("10", 60))
    assert body == {"broadcaster_id": "10", "length": 60}


@respx.mock
async def test_create_marker_posts_user_id():
    _, _, body = await _call_once(
        "POST", "/streams/markers", OK, lambda h: h.create_marker("10"))
    assert body == {"user_id": "10"}


@respx.mock
async def test_modify_channel_sends_only_given_field():
    _, params, body = await _call_once(
        "PATCH", "/channels", NO_CONTENT, lambda h: h.modify_channel("10", title="New title"))
    assert params == {"broadcaster_id": "10"}
    assert body == {"title": "New title"}
    respx.reset()
    _, _, body = await _call_once(
        "PATCH", "/channels", NO_CONTENT, lambda h: h.modify_channel("10", game_id="509658"))
    assert body == {"game_id": "509658"}


@respx.mock
async def test_search_categories_returns_id_and_name_in_order():
    result, params, _ = await _call_once(
        "GET", "/search/categories",
        httpx.Response(200, json={"data": [
            {"id": "1", "name": "Just Chatting", "box_art_url": "x"},
            {"id": "2", "name": "Chatting Sims", "box_art_url": "y"}]}),
        lambda h: h.search_categories("chat"))
    assert params == {"query": "chat"}
    assert result == [{"id": "1", "name": "Just Chatting"}, {"id": "2", "name": "Chatting Sims"}]


@respx.mock
async def test_list_moderators_follows_pages():
    def page(request):
        after = request.url.params.get("after")
        if after is None:
            return httpx.Response(200, json={
                "data": [{"user_login": "a"}, {"user_login": "b"}],
                "pagination": {"cursor": "c1"}})
        return httpx.Response(200, json={"data": [{"user_login": "c"}], "pagination": {}})
    route = respx.get(f"{BASE}/moderation/moderators").mock(side_effect=page)
    _, helix = make_helix(["tok-1"])
    try:
        names = await helix.list_moderators("10")
    finally:
        await helix.close()
    assert names == ["a", "b", "c"]
    assert dict(route.calls[0].request.url.params) == {"broadcaster_id": "10", "first": "100"}
    assert route.calls[1].request.url.params["after"] == "c1"


@respx.mock
async def test_list_vips_returns_logins():
    respx.get(f"{BASE}/channels/vips").mock(return_value=httpx.Response(
        200, json={"data": [{"user_login": "v1"}], "pagination": {}}))
    _, helix = make_helix(["tok-1"])
    try:
        assert await helix.list_vips("10") == ["v1"]
    finally:
        await helix.close()


@respx.mock
@pytest.mark.parametrize("http,path,invoke", [
    ("PATCH", "/chat/settings", lambda h: h.update_chat_settings("1", "2", emote_mode=True)),
    ("PUT", "/moderation/shield_mode", lambda h: h.set_shield_mode("1", "2", True)),
    ("POST", "/raids", lambda h: h.start_raid("1", "3")),
])
async def test_helix_error_carries_twitch_message(http, path, invoke):
    respx.route(method=http, url=f"{BASE}{path}").mock(return_value=httpx.Response(
        400, json={"error": "Bad Request", "status": 400, "message": "Twitch says no."}))
    _, helix = make_helix(["tok-1"])
    with pytest.raises(HelixError, match="Twitch says no."):
        try:
            await invoke(helix)
        finally:
            await helix.close()
