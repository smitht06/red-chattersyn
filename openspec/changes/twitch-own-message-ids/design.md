# Design: twitch-own-message-ids

## Context (facts checked 2026-11-10)

- `TwitchIrc.send` tags each PRIVMSG with a random `client-nonce`, shows the
  optimistic Message (id = the nonce) and keeps it in `_pending_nonces` for
  `NONCE_TTL` (60 s); `_handle_message` adopts the real id when a PRIVMSG echo with
  that nonce arrives. Twitch never echoes a user's own PRIVMSG, so that path never
  runs.
- After a PRIVMSG is accepted, Twitch sends USERSTATE for that channel with an `id`
  tag (the message id). USERSTATE on JOIN has no `id`. A refused PRIVMSG gets a
  NOTICE whose `msg-id` starts with `msg_` (msg_duplicate, msg_ratelimit,
  msg_slowmode, msg_emoteonly, …) instead.
- The optimistic Message object is the one shown in the tab, so changing its `id`
  updates every id lookup (ClearMsg's `mark_deleted`, /reply, right-click delete).

## Goals / Non-Goals

Goal: rows of messages the user sent carry Twitch's real id (D1).
Non-Goals: marking refused rows "not sent" (D2); guarding reply/delete before the
id arrives (D3); rewriting chat-log lines (D4).

## Open questions

None.

## Decision log

<!-- grillspec: depth=quick council=no budget=5 asked=3 -->

### Open

### Settled

- [x] D1 Matching — **in send order, per channel**: each sent message takes the id of the next USERSTATE carrying an `id` tag for that channel; a send-refusal NOTICE for that channel consumes the oldest waiting send instead; USERSTATE without `id` (on JOIN) is ignored · accepted · alternatives: none that work (USERSTATE has no nonce) · reversible: cheap
- [x] D2 Refused send — **keep today's behaviour**: the row stays and Twitch's refusal NOTICE row explains it · accepted · alternatives: mark the row "not sent" (new UI) · reversible: cheap
- [x] D3 Reply/delete before the id arrives — **no special handling**; Twitch refuses with its own message · accepted · reversible: cheap
- [x] D4 Fill-ins (no product effect): remove the dead client-nonce echo code; chat-log lines are not rewritten (default format has no {id}); a waiting send keeps waiting at most 60 s (today's NONCE_TTL) · agent's call · reversible: cheap
