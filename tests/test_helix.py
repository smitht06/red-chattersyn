"""Helix client: app token caching/refresh, badges, emotes, users."""

import httpx
import respx

from chattersyn.models.message import Badge
from chattersyn.services.twitch.helix import BASE, TOKEN_URL, Helix, HelixError

TOKEN_RESPONSE = {"access_token": "tok-1", "expires_in": 3600}


@respx.mock
async def test_token_fetched_once_and_reused():
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    api_route = respx.get(f"{BASE}/chat/badges/global").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    helix = Helix("cid", "secret")
    try:
        await helix.global_badges()
        await helix.global_badges()
    finally:
        await helix.close()
    assert token_route.call_count == 1
    assert api_route.call_count == 2
    assert api_route.calls[0].request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
async def test_token_refreshed_after_expiry():
    token_route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "expires_in": 0}),
            httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600}),
        ]
    )
    respx.get(f"{BASE}/chat/badges/global").mock(return_value=httpx.Response(200, json={"data": []}))
    helix = Helix("cid", "secret")
    try:
        await helix.global_badges()
        await helix.global_badges()
    finally:
        await helix.close()
    assert token_route.call_count == 2


@respx.mock
async def test_401_forces_single_refresh_then_retry():
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )
    api_route = respx.get(f"{BASE}/chat/badges/global").mock(
        side_effect=[
            httpx.Response(401, json={"error": "Unauthorized"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    helix = Helix("cid", "secret")
    try:
        await helix.global_badges()
    finally:
        await helix.close()
    assert token_route.call_count == 2  # first token + forced refresh
    assert api_route.call_count == 2


@respx.mock
async def test_badges_parsed_into_lookup():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
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
    helix = Helix("cid", "secret")
    try:
        badges = await helix.global_badges()
    finally:
        await helix.close()
    assert isinstance(badges["moderator"]["1"], Badge)
    assert badges["moderator"]["1"].title == "Moderator"
    assert badges["moderator"]["1"].urls["4x"] == "https://x/m4.png"


@respx.mock
async def test_channel_emotes_and_user():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
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
    helix = Helix("cid", "secret")
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
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(f"{BASE}/chat/emotes/global").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "1", "name": "BibleThump"}, {"id": "25", "name": "Kappa"}]}
        )
    )
    helix = Helix("cid", "secret")
    try:
        assert await helix.global_emotes() == {"1": "BibleThump", "25": "Kappa"}
    finally:
        await helix.close()


@respx.mock
async def test_user_follows_and_channel_followers():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(f"{BASE}/users/follows", params={"from_id": "1", "to_id": "2"}).mock(
        return_value=httpx.Response(
            200,
            json={"total": 1, "data": [
                {"from_id": "1", "to_id": "2", "followed_at": "2020-05-01T00:00:00Z"}]},
        )
    )
    respx.get(f"{BASE}/users/follows", params={"from_id": "1", "to_id": "9"}).mock(
        return_value=httpx.Response(200, json={"total": 0, "data": []})
    )
    respx.get(f"{BASE}/channels/followers", params={"broadcaster_id": "2"}).mock(
        return_value=httpx.Response(200, json={"total": 12345})
    )
    respx.get(f"{BASE}/channels/followers", params={"broadcaster_id": "9"}).mock(
        return_value=httpx.Response(200, json={"total": "not-a-number"})
    )
    helix = Helix("cid", "secret")
    try:
        assert await helix.user_follows("1", "2") == "2020-05-01T00:00:00Z"
        assert await helix.user_follows("1", "9") is None
        assert await helix.channel_followers("2") == 12345
        assert await helix.channel_followers("9") is None
    finally:
        await helix.close()


@respx.mock
async def test_missing_access_token_raises():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"expires_in": 3600}))
    helix = Helix("cid", "secret")
    try:
        try:
            await helix.global_badges()
            raise AssertionError("expected HelixError")
        except HelixError:
            pass
    finally:
        await helix.close()
