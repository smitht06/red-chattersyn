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
async def test_user_follows_and_channel_followers():
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
    _, helix = make_helix(["tok-1"])
    try:
        assert await helix.user_follows("1", "2") == "2020-05-01T00:00:00Z"
        assert await helix.user_follows("1", "9") is None
        assert await helix.channel_followers("2") == 12345
        assert await helix.channel_followers("9") is None
    finally:
        await helix.close()
