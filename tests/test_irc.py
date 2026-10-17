"""TwitchIrc against the in-process fake IRC server: connect/join, send with
nonce dedupe, ACTION/reply tags, PING, reconnect + rejoin, backoff, caps."""

import asyncio
import re
import time

import pytest
from fakeirc import FakeIrcServer

from chattersyn.models.message import MessageFlags
from chattersyn.services.twitch.irc import NotAuthenticated, TwitchIrc
from chattersyn.services.twitch.mapper import AuthFailed, TwitchMapper

NONCE_RE = re.compile(r"client-nonce=([0-9a-f]{32})")

GLOBALUSERSTATE = (
    "@badge-info=;badges=;color=#0D4200;display-name=TestUser;emote-sets=0;"
    "user-id=999;user-type= :tmi.twitch.tv GLOBALUSERSTATE"
)
PRIVMSG_VIEWER = (
    "@badges=;color=#FF0000;display-name=Viewer;emotes=;id=m1;mod=0;room-id=1;"
    "subscriber=0;tmi-sent-ts=1620000000000;turbo=0;user-id=9;user-type= "
    ":viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #forsen :hey"
)


def make_client(server, **kwargs):
    msgs, events, auths = [], [], []
    kwargs.setdefault("backoff_base", 0.05)
    kwargs.setdefault("send_spacing", 0.05)
    client = TwitchIrc(
        TwitchMapper(),
        ws_url=server.url,
        on_message=msgs.append,
        on_event=events.append,
        on_auth=auths.append,
        **kwargs,
    )
    return client, msgs, events, auths


async def wait_until(pred, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.01)


async def welcome(server, nick):
    await server.feed(f":tmi.twitch.tv 001 {nick} :Welcome, GLHF!")
    await server.feed(GLOBALUSERSTATE)


# -- connect / join -----------------------------------------------------------------


async def test_connect_caps_nick_join_and_message_flow():
    server = FakeIrcServer()
    await server.start()
    client, msgs, _, _ = make_client(server, nick="justinfan12345")
    client.start()
    try:
        caps = await server.wait_for(lambda ln: ln.startswith("CAP REQ"))
        assert "twitch.tv/tags" in caps
        assert "twitch.tv/commands" in caps
        assert "twitch.tv/membership" in caps
        await server.wait_for(lambda ln: ln == "NICK justinfan12345")
        assert client.is_anonymous
        assert client.join("forsen")
        await welcome(server, "justinfan12345")
        await server.wait_for(lambda ln: ln == "JOIN #forsen")
        # GLOBALUSERSTATE identity captured
        assert client.self_user_id == "999"
        assert client.display_name == "TestUser"
        assert client.color == "#0D4200"
        # a normal message flows through the mapper to on_message
        await server.feed(PRIVMSG_VIEWER)
        await wait_until(lambda: msgs)
        assert msgs[0].author.name == "Viewer"
        assert msgs[0].plain == "hey"
    finally:
        await client.stop()
        await server.stop()


async def test_token_sends_pass_and_anonymous_cannot_send():
    server = FakeIrcServer()
    await server.start()
    client, _, _, _ = make_client(server, nick="testuser", token="abc123")
    client.start()
    try:
        await server.wait_for(lambda ln: ln == "PASS oauth:abc123")
        await server.wait_for(lambda ln: ln == "NICK testuser")
        assert not client.is_anonymous
        await welcome(server, "testuser")
    finally:
        await client.stop()
        await server.stop()

    # Anonymous clients may read but never send.
    server2 = FakeIrcServer()
    await server2.start()
    anon, _, _, _ = make_client(server2, nick="justinfan12345")
    anon.start()
    try:
        await server2.wait_for(lambda ln: ln.startswith("NICK justinfan"))
        await welcome(server2, "justinfan12345")
        with pytest.raises(NotAuthenticated):
            await anon.send("forsen", "hi")
    finally:
        await anon.stop()
        await server2.stop()


# -- send ---------------------------------------------------------------------------


async def test_send_optimistic_with_nonce_and_echo_dedupe():
    server = FakeIrcServer()
    await server.start()
    client, msgs, _, _ = make_client(server, nick="testuser", token="tok")
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        await welcome(server, "testuser")
        await wait_until(lambda: client.display_name == "TestUser")

        msg = await client.send("forsen", "hello world")
        assert msg.plain == "hello world"
        assert msg.author.name == "TestUser"  # GLOBALUSERSTATE display-name
        assert msg.flags == MessageFlags.NONE

        line = await server.wait_for(lambda ln: "PRIVMSG #forsen :hello world" in ln)
        nonce = NONCE_RE.search(line).group(1)
        assert msg.id == nonce

        # Server echoes our message back with the nonce + a real id: deduped.
        await server.feed(
            f"@badges=;color=#0D4200;client-nonce={nonce};display-name=TestUser;emotes=;"
            f"id=server-42;mod=0;room-id=1;subscriber=0;tmi-sent-ts=1620000000000;"
            f"turbo=0;user-id=999;user-type= "
            f":testuser!testuser@testuser.tmi.twitch.tv PRIVMSG #forsen :hello world"
        )
        await wait_until(lambda: msg.id == "server-42")
        assert msgs == []  # nothing double-published

        # Unrelated message still surfaces.
        await server.feed(PRIVMSG_VIEWER)
        await wait_until(lambda: msgs)
        assert msgs[0].id == "m1"
    finally:
        await client.stop()
        await server.stop()


async def test_send_action_reply_tags_and_newline_sanitizing():
    server = FakeIrcServer()
    await server.start()
    client, _, _, _ = make_client(server, nick="testuser", token="tok")
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        await welcome(server, "testuser")

        reply = await client.send(
            "forsen", "/me waves", reply_parent_id="p1", reply_parent_login="poster"
        )
        assert reply.flags & MessageFlags.ACTION
        line = await server.wait_for(lambda ln: "\x01ACTION waves\x01" in ln)
        assert "reply-parent-msg-id=p1" in line
        assert "reply-parent-user-login=poster" in line

        await client.send("forsen", "line1\nline2")
        await server.wait_for(lambda ln: "PRIVMSG #forsen :line1 line2" in ln)
    finally:
        await client.stop()
        await server.stop()


async def test_send_min_interval_spacing():
    server = FakeIrcServer()
    await server.start()
    client, _, _, _ = make_client(server, nick="testuser", token="tok", send_spacing=0.25)
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        await welcome(server, "testuser")
        await client.send("forsen", "one")
        await server.wait_for(lambda ln: "PRIVMSG #forsen :one" in ln)
        t0 = time.monotonic()
        await client.send("forsen", "two")
        await server.wait_for(lambda ln: "PRIVMSG #forsen :two" in ln)
        assert time.monotonic() - t0 >= 0.2
    finally:
        await client.stop()
        await server.stop()


# -- protocol plumbing ----------------------------------------------------------------


async def test_ping_is_answered_with_pong():
    server = FakeIrcServer()
    await server.start()
    client, _, _, _ = make_client(server, nick="testuser")
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        await server.feed("PING :tmi.twitch.tv")
        await server.wait_for(lambda ln: ln == "PONG :tmi.twitch.tv")
    finally:
        await client.stop()
        await server.stop()


async def test_auth_failed_notice_routed_to_on_auth():
    server = FakeIrcServer()
    await server.start()
    client, _, _, auths = make_client(server, nick="testuser", token="bad")
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        await server.feed(":tmi.twitch.tv NOTICE * :Login unsuccessful")
        await wait_until(lambda: auths)
        assert isinstance(auths[0], AuthFailed)
        assert auths[0].reason == "Login unsuccessful"
    finally:
        await client.stop()
        await server.stop()


async def test_join_cap_at_90():
    client, _, _, _ = make_client(FakeIrcServer(), nick="x")
    for i in range(90):
        assert client.join(f"chan{i}")
    assert not client.join("chan90")
    assert len(client._channels) == 90


# -- reconnect ------------------------------------------------------------------------


async def test_reconnect_command_drops_and_rejoins():
    server = FakeIrcServer()
    await server.start()
    client, _, _, _ = make_client(server, nick="testuser")
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        assert client.join("forsen")
        await welcome(server, "testuser")
        await server.wait_for(lambda ln: ln == "JOIN #forsen")

        await server.feed(":tmi.twitch.tv RECONNECT")
        # Client reconnects: second NICK on a new socket...
        await wait_until(lambda: server.count(lambda ln: ln.startswith("NICK")) >= 2)
        await welcome(server, "testuser")
        # ...and re-joins the channel.
        await wait_until(lambda: server.count(lambda ln: ln == "JOIN #forsen") >= 2)
    finally:
        await client.stop()
        await server.stop()


async def test_connection_drop_reconnects_with_backoff():
    server = FakeIrcServer()
    await server.start()
    client, _, _, _ = make_client(server, nick="testuser")
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        assert client.join("forsen")
        await welcome(server, "testuser")
        await server.wait_for(lambda ln: ln == "JOIN #forsen")

        await server.close_all()  # simulate wifi kill
        await wait_until(lambda: server.count(lambda ln: ln.startswith("NICK")) >= 2, timeout=5)
        await welcome(server, "testuser")
        await wait_until(lambda: server.count(lambda ln: ln == "JOIN #forsen") >= 2)
    finally:
        await client.stop()
        await server.stop()


async def test_stop_closes_socket():
    server = FakeIrcServer()
    await server.start()
    client, _, _, _ = make_client(server, nick="testuser")
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        await client.stop()
        await wait_until(lambda: not server.connections)
    finally:
        await client.stop()
        await server.stop()
