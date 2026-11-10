# twitch-usercard-follows — plan

## Goal

Signed-in Twitch user cards show their avatar, account age and follower count
again, and mods/broadcasters see when the chatter followed the channel.

## Architecture

`Helix.channel_follower_since` replaces the removed `/users/follows` lookup with
`/channels/followers?broadcaster_id=&user_id=`. `SCOPES` gains
`moderator:read:followers`. MainWindow's Twitch enrichment runs each lookup on its
own and passes each result to the card as it arrives; the card's follow row takes
its label from the card's channel.

**Spec:** openspec/changes/twitch-usercard-follows/specs/twitch-usercard/spec.md

## Global Constraints

- Commit or push only when the user asks; never add a `Co-Authored-By` trailer (CLAUDE.md).
- Tests use no credentials and no network: respx for Helix, fakes for MainWindow.
- Exact texts: follow row label "Followed <channel> since:" (date shown as YYYY-MM-DD); scope string `moderator:read:followers`.
- No reconnect prompt for the new scope; YouTube cards unchanged.
- `uv run ruff check src/ tests/` and `uv run pytest -q` stay green after every task.

## Review Focus

1. The card may close (or be replaced) while a lookup is in flight; a late answer must not write into a closed or different card → task 2.1 pins `test_late_answer_after_card_replaced_is_dropped`.
2. A token whose scopes were synced from /validate without the new scope must not send the follow lookup (Twitch answers 401, and a 401 makes Helix re-pull the token) → task 2.1 pins it in the TUF-7 test.

## 1. Helix and scopes

- [x] 1.1 Follow lookup on /channels/followers and the new scope — verify: uv run pytest tests/test_helix.py tests/test_twitch_auth.py -q passes

Covers: TUF-9 · Test: scenario-first

Files: modify src/chattersyn/services/twitch/helix.py, src/chattersyn/services/twitch/auth.py; test tests/test_helix.py, tests/test_twitch_auth.py

Interfaces:
- Produces in auth.py: `FOLLOWERS_SCOPE = "moderator:read:followers"`; `SCOPES = ["chat:read", "chat:edit", MODERATION_SCOPE, FOLLOWERS_SCOPE]`.
- Produces in helix.py: `channel_follower_since(broadcaster_id: str, user_id: str) -> str | None` — GET `/channels/followers` with `broadcaster_id` and `user_id`; returns `data[0]["followed_at"]` or None when `data` is empty. `user_follows` and its `/users/follows` call are removed.

1. Replace `test_user_follows_and_channel_followers` with `test_channel_follower_since` (followed → "2024-03-05T10:00:00Z"; empty data → None) and `test_channel_followers_total` (12345; non-numeric → None). Add `test_sign_in_requests_follow_scope` (the authorize URL's `scope` contains all four scopes). Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `fix(twitch): follow lookup on /channels/followers, request moderator:read:followers`.

## 2. User card

- [x] 2.1 Independent lookups and the channel follow row — verify: uv run pytest tests/test_usercard.py tests/test_main_window_usercard_enrich.py -q passes

Covers: TUF-1, TUF-2, TUF-3, TUF-4, TUF-5, TUF-6, TUF-7, TUF-8 · Test: scenario-first

Files: modify src/chattersyn/ui/main_window.py, src/chattersyn/ui/usercard.py; test tests/test_usercard.py, tests/test_main_window_usercard_enrich.py (new; fake Twitch service with a respx-backed Helix, same fixture style as the other tests/test_main_window_*.py)

Interfaces:
- Consumes: `channel_follower_since`, `FOLLOWERS_SCOPE` (1.1); `ModerationState.has_twitch_rights(channel)`.
- `UsercardPopup.set_details(..., followage: str = "", followage_channel: str = "")`: with `followage_channel` the row label becomes "Followed <followage_channel> since:"; `set_details` is called once per arriving detail (rows already shown stay).
- `_enrich_twitch_usercard(popup, target, channel: str, self_is_mod: bool)`: after `user_by_login` (avatar + creation date set at once), the follower total and the follow lookup each run in their own try/except and each call `set_details` when they answer and `self._usercard_popup is popup`. The follow lookup runs only when `self_is_mod`, the token's `scopes` contain `FOLLOWERS_SCOPE`, the channel's broadcaster id is known (`user_by_login(channel)`), and the target is not that broadcaster. The caller passes the tab's `twitch_channel` and the `self_is_mod` it already computes.

1. Write tests: `test_card_fills_in_when_signed_in` (TUF-1), `test_follow_lookup_failure_keeps_other_details` (TUF-2: follow route 500), `test_follower_count_failure_keeps_other_details` (TUF-3), `test_mod_sees_followed_since` (TUF-4: label "Followed alice since:", value "2024-03-05"), `test_not_following_shows_no_row` (TUF-5), `test_no_mod_rights_sends_no_follow_lookup` (TUF-6: route call_count 0), `test_missing_scope_sends_no_follow_lookup` (TUF-7), `test_broadcaster_card_sends_no_follow_lookup` (TUF-8), `test_late_answer_after_card_replaced_is_dropped` (Review Focus 1), plus a usercard unit test for the label. Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run ruff check src/ tests/` and `uv run pytest -q`. Expected: PASS.
4. Commit `fix(ui): Twitch user cards fill in again; follow date for the tab's channel`.
