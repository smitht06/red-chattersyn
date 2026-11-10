# Design: twitch-usercard-follows

## Context (facts checked 2026-11-10)

- `MainWindow._enrich_twitch_usercard` runs `user_by_login`, `channel_followers`
  and `user_follows` in sequence and calls `popup.set_details` once at the end; the
  caller swallows any exception, so one failure drops every detail.
- `Helix.user_follows` calls `/users/follows` (removed by Twitch 2023-09-06).
  Its replacement for "does user X follow channel B" is
  `GET /channels/followers?broadcaster_id=B&user_id=X`, which needs the
  `moderator:read:followers` scope and a token user who is B or a mod of B. The
  same endpoint's `total` is returned to any user token without that scope.
- `UsercardPopup.set_details(created_at, followers, followage, avatar_url)` shows
  each row only when its value is given; the follow row's label is fixed to
  "Follows you since:".
- `ModerationState.has_twitch_rights(channel)` is True for the broadcaster and
  mods of that channel; `SCOPES` in `services/twitch/auth.py` is
  chat:read, chat:edit, moderator:manage:banned_users; `ensure_valid` syncs the
  token's scopes from /validate.

## Goals / Non-Goals

Goals: signed-in cards show avatar, account age and follower count again (D3, D4);
mods and broadcasters see when the user followed the channel (D1, D2).

Non-Goals: a reconnect prompt for the new permission (D2); YouTube cards.

## Open questions

None.

## Decision log

<!-- grillspec: depth=quick council=no budget=5 asked=4 -->

### Open

### Settled

- [x] D1 Follow row — **"Followed <channel> since: <date>"**: when the card's user followed the channel of the tab the card was opened from; shown only when the signed-in user is that channel's broadcaster or a mod there (Twitch answers only them); on your own channel this is "follows you" · accepted · alternatives: always "Follows you since" (own channel, any tab); drop the row · reversible: cheap
- [x] D2 Permission — **add moderator:read:followers**; existing users reconnect Twitch once (release notes "Action needed"); until then the row stays hidden, no nag (same pattern as the v0.6.0 ban scope) · accepted · alternatives: no new scope (row can never be filled) · reversible: costly (users re-consent)
- [x] D3 Failure isolation — **each detail loads on its own**: a failing follow lookup (or any other) never hides the avatar, account age or follower count · accepted · reversible: cheap
- [x] D4 Follower total — **keep /channels/followers' total** (any signed-in token, no new scope); replace the test that mocked the removed /users/follows as working · accepted · reversible: cheap
- [x] D5 Fill-ins (no product effect): no follow lookup on the card of the channel's own broadcaster (a channel can't follow itself); no lookup when the token lacks the follow permission (Twitch would answer 401); rows fill in as each lookup answers · agent's call · reversible: cheap
