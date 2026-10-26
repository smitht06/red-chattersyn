"""Offscreen render + hit-test smoke tests for the message delegate."""

import base64
import datetime as dt

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QUrl
from PySide6.QtGui import QImage, QMovie, QTextDocument

from chattersyn.config.settings import Settings
from chattersyn.core.image_cache import image_cache
from chattersyn.models.emote import Emote
from chattersyn.models.message import Author, Badge, ImageRun, Message, MessageFlags, Platform, TextRun
from chattersyn.ui import theme
from chattersyn.ui.chat_view import ChatView
from chattersyn.ui.message_delegate import MessageDelegate


def _msg(platform: Platform, text: str, msg_id: str = "m1") -> Message:
    author = Author("alice", login="alice", color="#ff0000",
                    badges=[Badge("moderator", "1", "Moderator")])
    return Message(
        id=msg_id, platform=platform, channel_id="ch", ts=dt.datetime.now(),
        author=author, runs=[TextRun(text)], plain=text,
    )


def _emote_msg(msg_id: str = "m2") -> Message:
    author = Author("bob", login="bob", color="#1e90ff")
    return Message(
        id=msg_id, platform=Platform.TWITCH, channel_id="ch", ts=dt.datetime.now(),
        author=author,
        runs=[TextRun("nice "), ImageRun(Emote("1", "Kappa", "twitch", height_px=28), "Kappa")],
        plain="nice Kappa",
    )


def _animated_emote_msg(url: str, msg_id: str = "m3") -> Message:
    author = Author("bob", login="bob", color="#1e90ff")
    emote = Emote("2", "KappaLoop", "7tv", urls={"1x": url}, animated=True, height_px=28)
    return Message(
        id=msg_id, platform=Platform.TWITCH, channel_id="ch", ts=dt.datetime.now(),
        author=author,
        runs=[TextRun("spin "), ImageRun(emote, "KappaLoop")],
        plain="spin KappaLoop",
    )


def _one_frame_gif_movie() -> QMovie:
    """A real QMovie wrapping a minimal 1-frame GIF, so the frame-flush path's
    isinstance/currentPixmap checks exercise genuine Qt objects offscreen."""
    gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")
    buf = QBuffer()
    buf.setData(QByteArray(gif))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    movie = QMovie(buf)
    movie.start()
    return movie


def test_render_smoke(qapp):
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    view.resize(600, 400)
    for i in range(20):
        msg = _msg(Platform.TWITCH if i % 2 else Platform.YOUTUBE,
                   f"message {i} with link https://example.com/{i}", f"m{i}")
        if i == 5:
            msg.flags = MessageFlags.HIGHLIGHTED
        view.message_model.add(msg)
    view.message_model.add(_emote_msg("m20"))
    view.message_model.flush()
    qapp.processEvents()

    image = QImage(600, 400, QImage.Format.Format_ARGB32)
    view.render(image)
    assert not image.isNull()

    painted = any(
        image.pixelColor(x, y).alpha() > 0
        for y in range(0, 400, 7)
        for x in range(0, 600, 7)
    )
    assert painted


def test_hit_test_username(qapp):
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    view.resize(600, 200)
    view.message_model.add(_msg(Platform.TWITCH, "some text"))
    view.message_model.flush()
    qapp.processEvents()

    index = view.message_model.index(0)
    rect = view.visualRect(index)
    # Scan the first line for the username hit, which sits after the platform chip.
    found = None
    for x in range(int(rect.x()) + 40, int(rect.x()) + 200, 5):
        hit = view._delegate.hit_test(index, QPointF(x, rect.y() + 8), rect)
        if hit and hit[0] == "user":
            found = hit
            break
    assert found == ("user", "alice")


def test_hit_test_link(qapp):
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    view.resize(600, 200)
    # Link tokenization happens upstream (IRC/YouTube mappers), so the model
    # receives a linked TextRun.
    author = Author("alice", login="alice", color="#ff0000")
    msg = Message(
        id="m1", platform=Platform.TWITCH, channel_id="ch", ts=dt.datetime.now(),
        author=author,
        runs=[TextRun("check "), TextRun("https://example.com", link="https://example.com"),
              TextRun(" out")],
        plain="check https://example.com out",
    )
    view.message_model.add(msg)
    view.message_model.flush()
    qapp.processEvents()

    index = view.message_model.index(0)
    rect = view.visualRect(index)
    found = None
    for x in range(int(rect.x()) + 60, int(rect.right()), 5):
        hit = view._delegate.hit_test(index, QPointF(x, rect.y() + 8), rect)
        if hit and hit[0] == "link":
            found = hit
            break
    assert found == ("link", "https://example.com")


def test_hit_test_past_text_end_is_no_hit(qapp):
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    view.resize(600, 200)
    view.message_model.add(_msg(Platform.TWITCH, "short"))
    view.message_model.flush()
    qapp.processEvents()

    index = view.message_model.index(0)
    rect = view.visualRect(index)
    hit = view._delegate.hit_test(index, QPointF(rect.right() - 10, rect.y() + 8), rect)
    assert hit is None


# -- highlight colour + layout cache keys --------------------------------------------------


def test_highlight_color_prefers_message_extra(qapp):
    delegate = MessageDelegate(Settings({"highlight_color": "#111111"}))
    msg = _msg(Platform.TWITCH, "hello")
    msg.flags = MessageFlags.HIGHLIGHTED
    assert delegate._highlight_color(msg) == "#111111"  # settings default
    msg.extra["highlight_color"] = "#ff0000"
    assert delegate._highlight_color(msg) == "#ff0000"  # matched entry colour wins


def test_layout_cache_key_includes_flags(qapp):
    delegate = MessageDelegate(Settings())
    msg = _msg(Platform.TWITCH, "hello")
    first = delegate._layout_for(msg, 500, 1.0)
    second = delegate._layout_for(msg, 500, 1.0)
    assert first is second  # unchanged flags -> cache hit
    msg.flags |= MessageFlags.DELETED
    third = delegate._layout_for(msg, 500, 1.0)
    assert third is not first  # flag change rebuilds (strikethrough must land)


def test_layout_cache_key_includes_plain_text(qapp):
    """The ClearChat restack mutates msg.plain in place ("(N times)"): the
    cache key must include the text, or restacked rows keep the old layout."""
    delegate = MessageDelegate(Settings())
    msg = _msg(Platform.TWITCH, "hello")
    first = delegate._layout_for(msg, 500, 1.0)
    msg.plain = "hello (2 times)"
    msg.runs = [TextRun(msg.plain)]
    second = delegate._layout_for(msg, 500, 1.0)
    assert second is not first  # same id/flags/width, new text -> rebuild


def test_disabled_veil_covers_row_without_timestamps(qapp):
    """Regression: with timestamps disabled and no mod buttons, the body
    translate stays active when the veil draws — it must be reset, or the
    overlay is displaced and the row's top-left strip is never darkened."""
    theme.apply(qapp, "dark")

    def render(disabled: bool) -> QImage:
        view = ChatView(Settings({"timestamps": {"enabled": False}}))
        view.resize(600, 100)
        msg = _msg(Platform.TWITCH, "hello")
        if disabled:
            msg.flags = MessageFlags.DISABLED
        view.message_model.add(msg)
        view.message_model.flush()
        qapp.processEvents()
        image = QImage(600, 100, QImage.Format.Format_ARGB32)
        view.render(image)
        return image

    def corner(image: QImage) -> int:
        color = image.pixelColor(2, 2)  # background only: left of chip and text
        return sum(color.getRgb()[:3])

    assert corner(render(True)) < corner(render(False))  # veil darkens the corner


# -- moderation-mode buttons ------------------------------------------------------------


def test_mod_buttons_hit_test(qapp):
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    view.resize(600, 200)
    msg = _emote_msg()  # bob: no protected badges
    view.message_model.add(msg)
    view.set_moderation_enabled(True)
    view.set_moderation_actions(["Ban"])
    view.message_model.flush()
    qapp.processEvents()

    layout = view._delegate._layout_for(msg, view.width(), 1.0)
    assert layout.buttons  # one button in the right gutter
    bx, by, bw, bh, label = layout.buttons[0]
    index = view.message_model.index(0)
    hit = view._delegate.hit_test(
        index, QPointF(bx + bw / 2, by + bh / 2), view.visualRect(index))
    assert hit == ("mod", "Ban")


def test_mod_buttons_hidden_when_mode_off(qapp):
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    view.resize(600, 200)
    view.set_moderation_actions(["Ban"])
    msg = _emote_msg()
    view.message_model.add(msg)
    view.message_model.flush()
    layout = view._delegate._layout_for(msg, view.width(), 1.0)
    assert layout.buttons == []


def test_mod_buttons_skip_protected_rows(qapp):
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    view.resize(600, 200)
    view.set_moderation_enabled(True)
    view.set_moderation_actions(["Ban"])
    msg = _msg(Platform.TWITCH, "hi")  # alice carries a moderator badge
    view.message_model.add(msg)
    view.message_model.flush()
    layout = view._delegate._layout_for(msg, view.width(), 1.0)
    assert layout.buttons == []


def test_youtube_rows_use_youtube_actions(qapp):
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    view.resize(600, 200)
    view.set_moderation_enabled(True)
    view.set_moderation_actions(["Ban"])
    view.set_youtube_actions(["Ban", "Timeout", "Hide"])
    msg = Message(
        id="m9", platform=Platform.YOUTUBE, channel_id="ch", ts=dt.datetime.now(),
        author=Author("bob", login="bob"),
        runs=[TextRun("hi")], plain="hi",
    )
    view.message_model.add(msg)
    view.message_model.flush()
    layout = view._delegate._layout_for(msg, view.width(), 1.0)
    assert [b[4] for b in layout.buttons] == ["Ban", "Timeout", "Hide"]


def test_youtube_owner_rows_skip_buttons(qapp):
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    view.resize(600, 200)
    view.set_moderation_enabled(True)
    view.set_youtube_actions(["Ban"])
    msg = Message(
        id="m10", platform=Platform.YOUTUBE, channel_id="ch", ts=dt.datetime.now(),
        author=Author("Owner", login="owner", badges=[Badge("yt", "owner", "Owner")]),
        runs=[TextRun("hi")], plain="hi",
    )
    view.message_model.add(msg)
    view.message_model.flush()
    layout = view._delegate._layout_for(msg, view.width(), 1.0)
    assert layout.buttons == []


# -- animated (GIF) emotes ------------------------------------------------------------


def test_animated_emote_tracked_in_layout(qapp):
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    view.resize(600, 200)
    url = "https://cdn.example/KappaLoop.gif"
    view.message_model.add(_animated_emote_msg(url))
    view.message_model.flush()
    qapp.processEvents()

    animated = view._delegate._layout_for(
        view.message_model.message_at(0), view.width(), 1.0).animated
    assert animated == [("emote://e/0", url)]  # first (only) emote image

    static = view._delegate._layout_for(
        _emote_msg(), view.width(), 1.0).animated
    assert static == []  # static PNG emotes never enter the frame-flush path


def test_frame_flush_overwrites_resource_and_clears_dirty(qapp, monkeypatch):
    theme.apply(qapp, "dark")
    url = "https://cdn.example/KappaLoop.gif"
    view = ChatView(Settings())
    view.resize(600, 200)
    view.message_model.add(_animated_emote_msg(url))
    view.message_model.flush()
    qapp.processEvents()

    delegate = view._delegate
    layout = delegate._layout_for(view.message_model.message_at(0), view.width(), 1.0)
    name = layout.animated[0][0]

    movie = _one_frame_gif_movie()
    qapp.processEvents()
    monkeypatch.setattr(image_cache, "request", lambda u, animated=False: movie)

    delegate._dirty_frames.add(url)
    delegate._flush_frames()
    assert delegate._dirty_frames == set()

    # With the movie gone, loadResource would serve the 56x56 placeholder —
    # only a resource stored by the flush yields the GIF's 1x1 frame.
    monkeypatch.setattr(image_cache, "request", lambda u, animated=False: None)
    stored = layout.doc.resource(
        QTextDocument.ResourceType.ImageResource, QUrl(name))
    assert stored is not None and (stored.width(), stored.height()) == (1, 1)


def test_on_frame_changed_coalesces_into_timer(qapp, monkeypatch):
    theme.apply(qapp, "dark")
    delegate = MessageDelegate(Settings())
    delegate._dirty_frames.add("https://cdn.example/other.gif")
    monkeypatch.setattr(image_cache, "request", lambda u, animated=False: None)
    delegate._on_frame_changed("https://cdn.example/KappaLoop.gif")
    assert "https://cdn.example/KappaLoop.gif" in delegate._dirty_frames
    assert delegate._frame_timer.isActive()  # single-shot repaint scheduled
    delegate._frame_timer.stop()  # don't leak a timer into later tests


def test_twitch_emotes_requested_as_possibly_animated(qapp):
    """IRC tags don't say which Twitch emotes animate (the CDN's "default"
    format serves a GIF when they do), so they take the animated path and
    the image cache decides by frame count."""
    theme.apply(qapp, "dark")
    view = ChatView(Settings())
    url = "https://static-cdn.jtvnw.net/emoticons/v2/1/default/dark/1.0"
    emote = Emote("1", "Kappa", "twitch", urls={"1x": url}, height_px=28)
    msg = Message(
        id="m4", platform=Platform.TWITCH, channel_id="ch", ts=dt.datetime.now(),
        author=Author("bob", login="bob"), runs=[ImageRun(emote, "Kappa")], plain="Kappa")
    layout = view._delegate._layout_for(msg, 600, 1.0)
    assert layout.animated == [("emote://e/0", url)]
