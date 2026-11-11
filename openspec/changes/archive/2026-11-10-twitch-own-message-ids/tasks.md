# twitch-own-message-ids — plan

## Goal

Give rows of messages the user sends on Twitch the real id from Twitch's USERSTATE
answer.

## Architecture

`TwitchIrc` replaces `_pending_nonces` with a per-channel FIFO of waiting sends.
`_handle_line` assigns the id of a USERSTATE carrying `id` to the oldest waiting
send of its channel and drops the oldest on a `msg_*` NOTICE, before the line is
mapped and dispatched as today. The dead echo path in `_handle_message` goes.

**Spec:** openspec/changes/twitch-own-message-ids/specs/twitch-own-messages/spec.md

## Global Constraints

- Commit or push only when the user asks; never add a `Co-Authored-By` trailer (CLAUDE.md).
- Tests use tests/fakeirc.py (in-process IRC server); no network.
- `client-nonce` stays on the PRIVMSG (Twitch accepts it); the optimistic id stays the nonce until the real id arrives.
- `uv run ruff check src/ tests/` and `uv run pytest -q` stay green.

## Review Focus

1. The id must be set on the very Message object the tab shows (no copy), or deletes still miss the row → task 1.1 pins `test_mod_delete_marks_own_row` through MainWindow's ClearMsg path.

## 1. Chat connection

- [x] 1.1 Waiting sends take USERSTATE ids — verify: uv run pytest tests/test_irc.py tests/test_main_window_moderation.py -q passes

Covers: OMI-1, OMI-2, OMI-3, OMI-4, OMI-5, OMI-6 · Test: scenario-first

Files: modify src/chattersyn/services/twitch/irc.py; test tests/test_irc.py, tests/test_main_window_moderation.py

Interfaces:
- `TwitchIrc._waiting: dict[str, collections.deque[tuple[float, Message]]]` (channel → (sent at, Message)); entries older than `NONCE_TTL` (60 s, monotonic `self._now()`) are dropped when the queue is read.
- `send` appends to the channel's queue (replacing `_pending_nonces` and its `call_later`); `_handle_line` handles USERSTATE-with-`id` and `msg_*` NOTICEs before mapping; `_handle_message` loses the nonce branch.

1. Write tests in tests/test_irc.py: `test_userstate_id_adopted` (OMI-1), `test_two_sends_in_order` (OMI-2), `test_refusal_consumes_oldest` (OMI-3: `@msg-id=msg_duplicate :tmi.twitch.tv NOTICE #alice :…`), `test_join_userstate_ignored` (OMI-4), `test_channels_kept_apart` (OMI-5), `test_waiting_send_expires` (injected `now` past 60 s → a later USERSTATE id changes nothing). Replace `test_send_optimistic_with_nonce_and_echo_dedupe` (echo path removed; keep its nonce-tag assertions). In tests/test_main_window_moderation.py add `test_mod_delete_marks_own_row` (OMI-6: own optimistic row given id "abc-1", ClearMsg "abc-1" → row marked deleted). Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run ruff check src/ tests/` and `uv run pytest -q`. Expected: PASS.
4. Commit `fix(twitch): own messages take their real id from USERSTATE`.
