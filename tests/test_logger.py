"""ChatLogger: formatting, per-channel files, flush, rollover, shutdown drain."""

import asyncio
import datetime as dt

import pytest

from chattersyn.config.settings import Settings
from chattersyn.core.bus import bridge
from chattersyn.models.message import Author, Message, Platform
from chattersyn.services.logger import ChatLogger, log_channel_key


def _msg(**kwargs):
    kw = dict(
        id="m1",
        platform=Platform.TWITCH,
        channel_id="forsen",
        ts=dt.datetime(2026, 10, 16, 9, 5, 7),
        author=Author(name="Alice"),
        plain="hello world",
    )
    kw.update(kwargs)
    return Message(**kw)


def _settings(tmp_path, **logging):
    return Settings({"logging": {"path": str(tmp_path), **logging}})


async def wait_until(predicate, timeout=2.0, step=0.005):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


@pytest.fixture
def logger(tmp_path):
    made = []

    def make(**logging):
        inst = ChatLogger(_settings(tmp_path, **logging))
        made.append(inst)
        return inst

    yield make
    for inst in made:  # safety net for tests that fail mid-run
        task = inst._writer_task
        if task is not None and not task.done():
            task.cancel()
        inst._close_files()


# -- log_channel_key -------------------------------------------------------------------


def test_log_channel_key():
    assert log_channel_key(_msg()) == "twitch-forsen"
    assert log_channel_key(_msg(channel_id="a/b c")) == "twitch-a_b_c"
    assert log_channel_key(_msg(channel_id="")) == "twitch-unknown"
    yt = _msg(platform=Platform.YOUTUBE, channel_id="Cg0K", extra={"video_id": "vid1"})
    assert log_channel_key(yt) == "youtube-vid1"
    yt_no_video = _msg(platform=Platform.YOUTUBE, channel_id="Cg0K")
    assert log_channel_key(yt_no_video) == "youtube-Cg0K"
    sys_msg = _msg(platform=Platform.SYSTEM, channel_id="forsen")
    assert log_channel_key(sys_msg) == "twitch-forsen"


# -- formatting --------------------------------------------------------------------------


def test_format_default(tmp_path):
    logger_inst = ChatLogger(_settings(tmp_path))
    assert logger_inst._format_line(_msg()) == "[09:05:07] Alice: hello world"


def test_format_custom_tokens(tmp_path):
    logger_inst = ChatLogger(
        _settings(tmp_path, format="{platform}/{channel} {name}: {msg} ({id})"))
    assert logger_inst._format_line(_msg()) == "twitch/forsen Alice: hello world (m1)"


def test_format_system_row(tmp_path):
    logger_inst = ChatLogger(_settings(tmp_path))
    sys_msg = Message.system(Platform.TWITCH, "forsen", "x was banned",
                             ts=dt.datetime(2026, 10, 16, 9, 5, 7))
    # the {name}: prefix from the format template stays; only {msg} is starred
    assert logger_inst._format_line(sys_msg) == "[09:05:07] : *x was banned*"


def test_format_12h_token(tmp_path):
    logger_inst = ChatLogger(_settings(tmp_path, format="[hh:mm] {msg}"))
    assert logger_inst._format_line(_msg()) == "[09:05] hello world"


def test_format_missing_author(tmp_path):
    logger_inst = ChatLogger(_settings(tmp_path))
    # an empty author name is NOT a system row — no stars, just an empty name
    assert logger_inst._format_line(_msg(author=Author(name=""))) == "[09:05:07] : hello world"


def test_logs_root_default_is_config_dir(tmp_path, monkeypatch):
    from chattersyn.services import logger as logger_mod

    monkeypatch.setattr(logger_mod, "config_dir", lambda: tmp_path / "conf")
    logger_inst = ChatLogger(Settings({}))
    assert logger_inst.logs_root == tmp_path / "conf" / "logs"


# -- file writing ------------------------------------------------------------------------


async def test_writes_per_channel_file(tmp_path, logger):
    log_inst = logger()
    log_inst._today = lambda: "2026-10-16"
    log_inst.start()
    bridge.message.emit(_msg())
    bridge.message.emit(_msg(platform=Platform.YOUTUBE, channel_id="Cg0K",
                             extra={"video_id": "vid1"}))
    await log_inst.stop()
    assert (tmp_path / "twitch-forsen" / "2026-10-16.log").read_text(
        encoding="utf-8") == "[09:05:07] Alice: hello world\n"
    assert (tmp_path / "youtube-vid1" / "2026-10-16.log").read_text(
        encoding="utf-8") == "[09:05:07] Alice: hello world\n"


async def test_disabled_writes_nothing(tmp_path, logger):
    log_inst = logger(enabled=False)
    log_inst._today = lambda: "2026-10-16"
    log_inst.start()
    bridge.message.emit(_msg())
    await log_inst.stop()
    assert not list(tmp_path.iterdir())


async def test_flush_without_stop(tmp_path, logger, monkeypatch):
    from chattersyn.services import logger as logger_mod

    monkeypatch.setattr(logger_mod, "_FLUSH_INTERVAL", 0.05)
    log_inst = logger()
    log_inst._today = lambda: "2026-10-16"
    log_inst.start()
    bridge.message.emit(_msg())
    path = tmp_path / "twitch-forsen" / "2026-10-16.log"
    assert await wait_until(path.exists)
    # the writer is still running; the flush interval must push the line to disk
    assert await wait_until(
        lambda: path.read_text(encoding="utf-8") == "[09:05:07] Alice: hello world\n")
    await log_inst.stop()


async def test_date_rollover(tmp_path, logger):
    log_inst = logger()
    log_inst._today = lambda: "2026-10-16"
    log_inst.start()
    bridge.message.emit(_msg())
    first = tmp_path / "twitch-forsen" / "2026-10-16.log"
    assert await wait_until(first.exists)
    log_inst._today = lambda: "2026-10-17"
    bridge.message.emit(_msg(plain="next day"))
    await log_inst.stop()
    assert (tmp_path / "twitch-forsen" / "2026-10-17.log").read_text(
        encoding="utf-8") == "[09:05:07] Alice: next day\n"


async def test_stop_flushes_tail(tmp_path, logger):
    log_inst = logger()
    log_inst._today = lambda: "2026-10-16"
    log_inst.start()
    bridge.message.emit(_msg())
    await log_inst.stop()  # immediate stop must still drain the line
    assert (tmp_path / "twitch-forsen" / "2026-10-16.log").read_text(
        encoding="utf-8") == "[09:05:07] Alice: hello world\n"


async def test_queue_overflow_drops_lines(tmp_path, logger, monkeypatch):
    from chattersyn.services import logger as logger_mod

    monkeypatch.setattr(logger_mod, "_QUEUE_LIMIT", 4)
    log_inst = logger()
    log_inst._today = lambda: "2026-10-16"
    log_inst.start()
    log_inst._writer_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await log_inst._writer_task
    log_inst._writer_task = None
    for i in range(8):  # > queue limit with the writer dead: lines are dropped
        log_inst._on_message(_msg(id=str(i), plain=f"msg {i}"))
    await log_inst.stop()
    assert not list(tmp_path.iterdir())
