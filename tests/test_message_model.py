import datetime as dt

from chattersyn.models.message import Author, Message, MessageFlags, Platform
from chattersyn.ui.message_model import MessageModel


def _msg(msg_id: str) -> Message:
    return Message.text(Platform.TWITCH, "forsen", Author("alice", login="alice"), "hello",
                        id=msg_id, ts=dt.datetime.now())


def test_batched_flush(qapp):
    model = MessageModel(max_messages=3)
    model.add(_msg("1"))
    model.add(_msg("2"))
    assert model.rowCount() == 0  # nothing until flush
    assert model.flush() == (2, 0)
    assert model.rowCount() == 2


def test_scrollback_cap(qapp):
    model = MessageModel(max_messages=3)
    for i in range(4):
        model.add(_msg(str(i)))
    added, trimmed = model.flush()
    assert (added, trimmed) == (4, 1)
    assert model.rowCount() == 3
    assert model.message_at(0).id == "1"  # oldest dropped


def test_data_roles(qapp):
    model = MessageModel()
    msg = _msg("1")
    model.add(msg)
    model.flush()
    index = model.index(0)
    assert index.data(MessageModel.MessageRole) is msg
    assert index.data(MessageModel.PlatformRole) == Platform.TWITCH
    assert index.data(MessageModel.PlainRole) == "hello"
    assert index.data(MessageModel.IdRole) == "1"


def test_clear(qapp):
    model = MessageModel()
    model.add(_msg("1"))
    model.flush()
    model.clear()
    assert model.rowCount() == 0
    assert model.flush() == (0, 0)


def test_mark_deleted_visible(qapp):
    model = MessageModel()
    msg = _msg("1")
    model.add(msg)
    model.flush()
    assert model.mark_deleted("1") is True
    assert msg.flags & MessageFlags.DELETED


def test_mark_deleted_pending(qapp):
    # CLEARMSG can arrive before the row is flushed — the flag must survive.
    model = MessageModel()
    msg = _msg("1")
    model.add(msg)
    assert model.mark_deleted("1") is True
    model.flush()
    assert msg.flags & MessageFlags.DELETED


def test_mark_deleted_unknown(qapp):
    model = MessageModel()
    assert model.mark_deleted("nope") is False


# -- moderation support ----------------------------------------------------------------


def _user_msg(msg_id, login):
    return Message.text(Platform.TWITCH, "forsen", Author(login, login=login),
                        "hello", id=msg_id, ts=dt.datetime.now())


def test_update_flags_for_user_sets_disabled(qapp):
    model = MessageModel()
    model.add(_user_msg("1", "bob"))
    model.add(_user_msg("2", "alice"))
    model.add(_user_msg("3", "bob"))
    model.flush()
    touched = model.update_flags_for_user("forsen", "Bob", MessageFlags.DISABLED)
    assert touched == 2
    assert model.message_at(0).flags & MessageFlags.DISABLED
    assert model.message_at(2).flags & MessageFlags.DISABLED
    assert not model.message_at(1).flags & MessageFlags.DISABLED


def test_update_flags_for_user_covers_pending(qapp):
    model = MessageModel()
    model.add(_user_msg("1", "bob"))
    touched = model.update_flags_for_user("forsen", "bob", MessageFlags.DISABLED)
    assert touched == 1
    model.flush()
    assert model.message_at(0).flags & MessageFlags.DISABLED


def test_update_flags_for_user_can_clear(qapp):
    model = MessageModel()
    model.add(_user_msg("1", "bob"))
    model.flush()
    model.update_flags_for_user("forsen", "bob", MessageFlags.DISABLED, enabled=False)
    assert not model.message_at(0).flags & MessageFlags.DISABLED


def test_update_flags_for_channel_grays_only_that_channel(qapp):
    """Full CLEARCHAT: every row in the channel, visible and pending."""
    model = MessageModel()
    model.add(_user_msg("1", "bob"))
    model.add(Message.text(Platform.TWITCH, "other", Author("A", login="a"), "hi", id="o1"))
    model.add(_user_msg("2", "alice"))
    model.flush()
    touched = model.update_flags_for_channel("forsen", MessageFlags.DISABLED)
    assert touched == 2
    assert model.message_at(0).flags & MessageFlags.DISABLED
    assert model.message_at(2).flags & MessageFlags.DISABLED
    assert not model.message_at(1).flags & MessageFlags.DISABLED
    model.add(_user_msg("3", "carol"))
    # Rows already flagged count as touched (idempotent set), plus the pending row.
    assert model.update_flags_for_channel("forsen", MessageFlags.DISABLED) == 3
    model.flush()
    assert model.message_at(3).flags & MessageFlags.DISABLED
    model.update_flags_for_channel("forsen", MessageFlags.DISABLED, enabled=False)
    assert not model.message_at(3).flags & MessageFlags.DISABLED


def test_update_message_mutates_visible_row(qapp):
    model = MessageModel()
    model.add(_msg("1"))
    model.flush()
    assert model.update_message("1", lambda m: setattr(m, "plain", "mutated")) is True
    assert model.message_at(0).plain == "mutated"


def test_update_message_mutates_pending_row(qapp):
    model = MessageModel()
    model.add(_msg("1"))
    assert model.update_message("1", lambda m: setattr(m, "plain", "mutated")) is True
    model.flush()
    assert model.message_at(0).plain == "mutated"


def test_update_message_unknown_id(qapp):
    model = MessageModel()
    assert model.update_message("nope", lambda m: None) is False
