"""TwitchIrc against the in-process fake IRC server: connect/join, send with
nonce dedupe, ACTION/reply tags, PING, reconnect + rejoin, backoff, caps."""

import asyncio
import re
import time

import pytest
from fakeirc import FakeIrcServer

from chattersyn.models.message import Author, Message, MessageFlags, Platform
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


async def test_send_optimistic_with_nonce():
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
        assert msg.id == nonce  # placeholder until Twitch's USERSTATE answers

        # Other people's messages still surface.
        await server.feed(PRIVMSG_VIEWER)
        await wait_until(lambda: msgs)
        assert msgs[0].id == "m1"
    finally:
        await client.stop()
        await server.stop()


# -- own message ids (USERSTATE answers) ---------------------------------------------------


def _userstate(channel, msg_id=None):
    tags = "badges=;color=#0D4200;display-name=TestUser;emote-sets=0;mod=0;subscriber=0;user-type="
    if msg_id:
        tags = f"{tags};id={msg_id}"
    return f"@{tags} :tmi.twitch.tv USERSTATE #{channel}"


async def _signed_in(**kwargs):
    server = FakeIrcServer()
    await server.start()
    client, msgs, events, _ = make_client(server, nick="testuser", token="tok", **kwargs)
    client.start()
    await server.wait_for(lambda ln: ln.startswith("NICK"))
    await welcome(server, "testuser")
    await wait_until(lambda: client.display_name == "TestUser")
    return server, client, events


async def _sent(server, client, channel, text):
    msg = await client.send(channel, text)
    await server.wait_for(lambda ln: f"PRIVMSG #{channel} :{text}" in ln)
    return msg


async def test_userstate_id_adopted():
    """OMI-1."""
    server, client, _ = await _signed_in()
    try:
        msg = await _sent(server, client, "alice", "hello")
        await server.feed(_userstate("alice", "abc-1"))
        await wait_until(lambda: msg.id == "abc-1")
    finally:
        await client.stop()
        await server.stop()


async def test_two_sends_in_order():
    """OMI-2."""
    server, client, _ = await _signed_in()
    try:
        one = await _sent(server, client, "alice", "one")
        two = await _sent(server, client, "alice", "two")
        await server.feed(_userstate("alice", "id-1"))
        await server.feed(_userstate("alice", "id-2"))
        await wait_until(lambda: two.id == "id-2")
        assert one.id == "id-1"
    finally:
        await client.stop()
        await server.stop()


async def test_refusal_consumes_oldest():
    """OMI-3."""
    server, client, _ = await _signed_in()
    try:
        one = await _sent(server, client, "alice", "one")
        placeholder = one.id
        two = await _sent(server, client, "alice", "two")
        await server.feed(
            "@msg-id=msg_duplicate :tmi.twitch.tv NOTICE #alice "
            ":Your message was not sent because it is identical to the previous one you sent.")
        await server.feed(_userstate("alice", "id-2"))
        await wait_until(lambda: two.id == "id-2")
        assert one.id == placeholder
    finally:
        await client.stop()
        await server.stop()


async def test_join_userstate_ignored():
    """OMI-4."""
    server, client, _ = await _signed_in()
    try:
        msg = await _sent(server, client, "alice", "hello")
        placeholder = msg.id
        await server.feed(_userstate("alice"))
        await asyncio.sleep(0.05)
        assert msg.id == placeholder
        await server.feed(_userstate("alice", "abc-1"))
        await wait_until(lambda: msg.id == "abc-1")
    finally:
        await client.stop()
        await server.stop()


async def test_channels_kept_apart():
    """OMI-5."""
    server, client, _ = await _signed_in()
    try:
        msg = await _sent(server, client, "alice", "hi")
        placeholder = msg.id
        await server.feed(_userstate("bob", "b-1"))
        await asyncio.sleep(0.05)
        assert msg.id == placeholder
        await server.feed(_userstate("alice", "a-1"))
        await wait_until(lambda: msg.id == "a-1")
    finally:
        await client.stop()
        await server.stop()


async def test_userstate_id_lands_on_the_shown_row():
    """Review Focus 1: the id goes onto the very Message object the tab shows."""
    server, client, _ = await _signed_in()
    try:
        shown = Message.text(Platform.TWITCH, "alice", Author(name="TestUser", login="testuser"),
                             "hello", id="optimistic-1")
        returned = await client.send("alice", "hello", optimistic=shown)
        assert returned is shown
        await server.wait_for(lambda ln: "PRIVMSG #alice :hello" in ln)
        await server.feed(_userstate("alice", "abc-1"))
        await wait_until(lambda: shown.id == "abc-1")
    finally:
        await client.stop()
        await server.stop()


async def test_waiting_send_expires():
    """A send with no answer within 60 s stops waiting and keeps its placeholder."""
    clock = [1000.0]
    server, client, _ = await _signed_in(now=lambda: clock[0])
    try:
        old = await _sent(server, client, "alice", "old")
        placeholder = old.id
        clock[0] += 61
        new = await _sent(server, client, "alice", "new")
        await server.feed(_userstate("alice", "n-1"))
        await wait_until(lambda: new.id == "n-1")
        assert old.id == placeholder
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


# -- connect reporting (on_connected / on_connect_failed) -----------------------------


def _closed_port_url() -> str:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"ws://127.0.0.1:{port}"  # nothing listens here once the socket closes


async def test_on_connected_called_on_welcome():
    server = FakeIrcServer()
    await server.start()
    connected, failed = [], []
    client, _, _, _ = make_client(
        server, nick="testuser",
        on_connected=lambda: connected.append(1), on_connect_failed=lambda: failed.append(1))
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        assert connected == []  # not before the 001
        await welcome(server, "testuser")
        await wait_until(lambda: connected)
        assert connected == [1]
        assert failed == []
    finally:
        await client.stop()
        await server.stop()


async def test_on_connect_failed_when_server_unreachable():
    failed = []
    client = TwitchIrc(
        TwitchMapper(), nick="testuser", token="tok", ws_url=_closed_port_url(),
        backoff_base=0.05, on_connect_failed=lambda: failed.append(1))
    client.start()
    try:
        await wait_until(lambda: len(failed) >= 2)  # every attempt reports, not just the first
    finally:
        await client.stop()


async def test_no_connect_failed_after_auth_notice():
    """A refused login is the service's re-login to handle, not an
    unreachable Twitch."""
    server = FakeIrcServer()
    await server.start()
    connected, failed = [], []
    client, _, _, auths = make_client(
        server, nick="testuser", token="bad",
        on_connected=lambda: connected.append(1), on_connect_failed=lambda: failed.append(1))
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        await server.feed(":tmi.twitch.tv NOTICE * :Login authentication failed")
        await wait_until(lambda: auths)
        await server.close_all()  # Twitch closes the socket after the notice
        # the reconnect means the refused attempt was fully handled
        await wait_until(lambda: server.count(lambda ln: ln.startswith("NICK")) >= 2)
        assert failed == []
        assert connected == []
    finally:
        await client.stop()
        await server.stop()


async def test_no_connect_failed_when_connection_drops_after_welcome():
    server = FakeIrcServer()
    await server.start()
    connected, failed = [], []
    client, _, _, _ = make_client(
        server, nick="testuser",
        on_connected=lambda: connected.append(1), on_connect_failed=lambda: failed.append(1))
    client.start()
    try:
        await server.wait_for(lambda ln: ln.startswith("NICK"))
        await welcome(server, "testuser")
        await wait_until(lambda: connected)
        await server.close_all()
        await wait_until(lambda: server.count(lambda ln: ln.startswith("NICK")) >= 2)
        assert failed == []  # it had connected; only attempts that never got a 001 count
    finally:
        await client.stop()
        await server.stop()


# -- backoff reset / callback safety (final review) -------------------------------------


async def test_backoff_grows_after_a_long_session_ends(monkeypatch, caplog):
    """A session that lasted > 30 s resets the backoff once - the failed
    attempts after it must grow again, not retry every second forever."""
    import logging

    from chattersyn.services.twitch import irc as irc_module

    monkeypatch.setattr(irc_module.random, "uniform", lambda a, b: 1.0)
    clock = [1000.0]
    client = TwitchIrc(TwitchMapper(), nick="testuser", now=lambda: clock[0],
                       backoff_base=0.001, backoff_cap=100.0)
    calls = [0]

    async def fake_connect():
        calls[0] += 1
        if calls[0] == 1:  # one welcomed session that lasts a minute
            client._welcome_time = clock[0]
            client._attempt_welcomed = True
            clock[0] += 60.0
            return
        clock[0] += 0.5  # then Twitch is unreachable: quick failures
        if calls[0] >= 5:
            client._stop_evt.set()
        raise OSError("unreachable")

    monkeypatch.setattr(client, "_connect_and_read", fake_connect)
    with caplog.at_level(logging.INFO, logger=irc_module.__name__):
        client.start()
        await asyncio.wait_for(client._run_task, 5)
    delays = [r.args[0] for r in caplog.records if r.msg == "reconnecting in %.1fs"]
    assert delays[:4] == pytest.approx([0.001, 0.002, 0.004, 0.008])


async def test_raising_connect_failed_callback_keeps_retrying():
    failed = []

    def boom():
        failed.append(1)
        raise RuntimeError("callback bug")

    client = TwitchIrc(
        TwitchMapper(), nick="testuser", token="tok", ws_url=_closed_port_url(),
        backoff_base=0.05, on_connect_failed=boom)
    client.start()
    try:
        await wait_until(lambda: len(failed) >= 2)
        assert not client._run_task.done()
    finally:
        await client.stop()
