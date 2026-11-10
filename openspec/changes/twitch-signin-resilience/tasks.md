# twitch-signin-resilience — plan

## Goal

Keep the saved Twitch login and a signed-in chat connection through Twitch outages,
offline launches and expired tokens, and fall back to read-only chat with a clear
message only when Twitch really rejects the sign-in.

## Architecture

`TwitchAuth` sorts every token check/refresh outcome into three results: valid
tokens, a definite rejection (`None`, saved login deleted) or `TwitchUnreachable`
(saved login kept); checks run one at a time under a lock. `TwitchIrc` reports
each welcome (001) and each connect attempt that failed before it. `TwitchService`
uses those to launch with the saved login when Twitch can't be reached, catch up on
every signed-in welcome, re-login once per IRC auth refusal, and fall back to
read-only. It reports reachability on the existing (unused) `bridge.connection_state`
signal and real rejections on `bridge.auth_state`; MainWindow renders both.

**Spec:** openspec/changes/twitch-signin-resilience/specs/twitch-signin/spec.md

## Global Constraints

- Commit or push only when the user asks; never add a `Co-Authored-By` trailer (CLAUDE.md).
- Tests use no credentials and no network: respx for HTTP, `tests/fakeirc.py` for IRC, fake services for MainWindow.
- User-facing texts exactly as the spec: "Twitch: can't reach Twitch — retrying…", "Twitch connected", "Twitch sign-in expired — reconnect your account", "Twitch rejected the sign-in — reconnect your account", Settings row "Not connected — <message>".
- Definite rejection = refresh answered 400 or 401, or an expired token (validate 401) with no refresh token. Unreachable = network error, timeout, 5xx, 429 (and any other non-200 refresh answer). Only a definite rejection deletes the saved login.
- No new timer or retry loop: retries ride on IRC's existing reconnect backoff and on later account actions.
- Never cancel a token refresh request mid-flight: Twitch refresh tokens of public clients (no secret) are single-use, so a cancelled refresh can lose the new token and the next refresh would delete the login. The launch stops *waiting* at 10 s; an in-flight refresh finishes and saves in the background.
- Behaviour the spec does not name stays as it is: the OAuth sign-in flow, `reconnect()` after "Connect Twitch", sign-out, anonymous emote loading, send rate limits.
- `uv run ruff check src/ tests/` and `uv run pytest -q` stay green after every task.

## Review Focus

1. A 10-s launch limit that cancels an in-flight refresh loses a single-use refresh token → task 2.1 pins `test_launch_timeout_lets_refresh_finish_and_save` (the refresh answers after the limit; the new tokens are saved).
2. Twitch sends the auth NOTICE and then closes the socket; IRC's own reconnect plus the service's re-login must not run two connections at once → task 2.3 pins `test_relogin_replaces_connection_once` (one live IRC client after a refusal + successful refresh).
3. Several auth NOTICEs in a row (or a NOTICE during a running re-login) must start one refresh, not several → task 2.3 pins it in `test_relogin_single_flight`.
4. A definite rejection reached through a Helix call (user card, ban) must also end in read-only + the expired message, not a silent `tokens = None` with a signed-in IRC → task 2.4 pins `test_helix_definite_rejection_falls_back`.
5. MainWindow's launch status ("Twitch connected (read-only — …)") must not overwrite the "retrying…" text set during `start()` → task 3.1 pins it in the TSR-10 test.

## 1. Token outcomes

- [ ] 1.1 Classify refresh/validate results and serialize checks — verify: uv run pytest tests/test_twitch_auth.py -q passes

Covers: TSR-1, TSR-2, TSR-3, TSR-4, TSR-5, TSR-23, TSR-24 · Test: scenario-first

Files: modify src/chattersyn/services/twitch/auth.py; test tests/test_twitch_auth.py

Interfaces:
- Produces in auth.py: `class TwitchUnreachable(Exception)`.
- `validate(access_token) -> dict | None`: None on 401; raises `TwitchUnreachable` on network errors/timeouts (`httpx.TransportError`), 5xx and 429.
- `refresh(tokens) -> TwitchTokens | None`: None on 400/401 (definite); raises `TwitchUnreachable` on network errors/timeouts and on any other non-200 answer.
- `ensure_valid(*, rejected_token: str | None = None) -> TwitchTokens | None`: runs under an `asyncio.Lock`; returns valid tokens, or None after deleting the saved login on a definite rejection; propagates `TwitchUnreachable` with the saved login untouched. With `rejected_token` (IRC refused it): if the saved access token still equals it, refresh without validating first (no refresh token → definite); if it differs, a refresh already happened — validate and return the current tokens.
- Inside `ensure_valid`, the refresh request plus `save_tokens` run in a task shielded from the caller's cancellation (`asyncio.shield`), so a caller that stops waiting never cancels them.

1. Write tests: `test_refresh_5xx_keeps_login` (validate 401, refresh 503 → `TwitchUnreachable`, store not deleted), `test_refresh_network_error_keeps_login` (respx side_effect `httpx.ConnectError`), `test_refresh_timeout_keeps_login` (`httpx.ReadTimeout`), `test_refresh_429_keeps_login`, `test_refresh_400_deletes_login` and `test_refresh_401_deletes_login` (→ None, TOKEN_KEY deleted), `test_expired_without_refresh_token_deletes_login`, `test_valid_without_refresh_token_kept` (validate 200, `refresh_token=""` → tokens returned, not deleted), `test_validate_5xx_and_network_raise_unreachable`, `test_concurrent_ensure_valid_one_refresh` (two `ensure_valid()` gathered, first validate 401 then 200 → exactly one POST to TOKEN_URL), `test_rejected_token_forces_refresh` (validate would answer 200, but `rejected_token="acc-1"` → one refresh), `test_rejected_token_already_replaced_skips_refresh`, `test_cancelled_caller_does_not_cancel_refresh` (cancel the awaiting caller while the refresh route is pending; after it answers, the new tokens are saved). Update `test_ensure_valid_clears_when_refresh_fails` only if its name now misleads (400 still deletes). Run. Expected: FAIL.
2. Implement to the interface.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS (callers in service.py still work: no test there drives a 5xx yet).
4. Commit `fix(twitch): keep the saved login unless Twitch rejects it`.

## 2. Service

- [ ] 2.1 Launch with the saved login and report reachability — verify: uv run pytest tests/test_twitch_service.py tests/test_irc.py -q passes

Covers: TSR-6, TSR-7, TSR-8, TSR-9, TSR-21 · Test: scenario-first

Files: modify src/chattersyn/services/twitch/irc.py, src/chattersyn/services/twitch/service.py; test tests/test_twitch_service.py, tests/test_irc.py

Interfaces:
- Consumes: `TwitchUnreachable`, `ensure_valid` (1.1).
- Produces in irc.py: `TwitchIrc(..., on_connected=None, on_connect_failed=None)`; `on_connected()` is called on each 001; `on_connect_failed()` is called when a connect attempt ends (exception or close) before its 001 — not after a refused login (an AuthFailed NOTICE was seen on that attempt).
- Produces in service.py: constants `LAUNCH_CHECK_TIMEOUT = 10.0`, `EXPIRED_MESSAGE = "Twitch sign-in expired — reconnect your account"`, `REJECTED_MESSAGE = "Twitch rejected the sign-in — reconnect your account"`; property `unreachable -> bool`; attribute `_check_pending: bool`.
- Reachability events: `bridge.connection_state.emit({"platform": "twitch", "channel_id": "", "state": "unreachable", "detail": ""})` when a connect attempt or a refresh can't reach Twitch (sets `unreachable`); `{"platform": "twitch", "channel_id": "", "state": "connected", "detail": "signed_in" | "anonymous"}` on every 001 (clears `unreachable`).
- `start()`: wait at most `LAUNCH_CHECK_TIMEOUT` for `ensure_valid()` (stop waiting, never cancel the refresh — see 1.1). Tokens → today's path. None → read-only and `bridge.auth_state.emit("twitch", False, EXPIRED_MESSAGE)` (replaces "stored tokens invalid — reconnect account"). Timeout or `TwitchUnreachable` → `self.tokens = self.auth.load_tokens()` (the saved login as stored), `_check_pending = True`, no Helix yet, emit "unreachable", connect IRC signed in. `start()` no longer raises for an unreachable Twitch.

1. Write irc tests: `test_on_connected_called_on_welcome`, `test_on_connect_failed_when_server_unreachable` (URL of a closed port), `test_no_connect_failed_after_auth_notice`. Service tests (respx for auth, a fake IRC factory or `fakeirc` for the connection): `test_launch_refreshes_expired_token_before_connect` (TSR-6: IRC built with the refreshed token), `test_launch_offline_connects_with_saved_login` (TSR-7: validate raises ConnectError → IRC built with nick=saved login and token=saved token; no exception; "unreachable" emitted), `test_launch_slow_check_connects_after_10s` (TSR-8: patch `LAUNCH_CHECK_TIMEOUT` small; the validate route never answers in time → IRC started), `test_launch_timeout_lets_refresh_finish_and_save` (Review Focus 1), `test_launch_5xx_keeps_login_and_connects` (TSR-9), `test_launch_rejected_login_goes_read_only` (TSR-21: refresh 400 → store deleted, anonymous IRC, auth_state False with EXPIRED_MESSAGE). Run. Expected: FAIL.
2. Implement the irc callbacks, then the service launch path and reachability events.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `fix(twitch): connect with the saved login when Twitch can't be reached at launch`.

- [ ] 2.2 Catch up on every signed-in welcome — verify: uv run pytest tests/test_twitch_service.py -q passes

Covers: TSR-12, TSR-13, TSR-14, TSR-22 · Test: scenario-first

Files: modify src/chattersyn/services/twitch/service.py; test tests/test_twitch_service.py

Interfaces:
- Consumes: `on_connected`, `_check_pending` (2.1).
- On a signed-in 001, `_catch_up()` runs (as a task): if `_check_pending`, `ensure_valid()` (tokens → clear the flag; None → the read-only fallback of 2.3 with EXPIRED_MESSAGE; `TwitchUnreachable` → stay pending, stop); then `ensure_helix()`; then the global badges/emotes if they have not loaded; then `_kick_metadata(name)` for each desired channel not in `_metadata_loaded`. Anonymous welcomes run nothing.
- `_load_channel_metadata` removes the channel from `_metadata_loaded` when its Helix part raised (a channel Twitch reports as missing, `user is None`, counts as loaded). `_load_global_badges` records success in `_global_badges_loaded: bool`.

1. Write tests: `test_catch_up_after_offline_launch` (TSR-12: launch offline → no Helix; simulate the welcome with the network back → validate called, Helix created, global badges requested), `test_catch_up_reloads_failed_channel` (TSR-13: channel metadata route 503 first, 200 after the next welcome → loaded), `test_catch_up_leaves_loaded_channels` (TSR-14: a second welcome sends no channel-metadata requests), `test_channel_opened_during_outage_loads_on_welcome` (TSR-22), `test_anonymous_welcome_runs_no_catch_up`. Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `fix(twitch): catch up account features and channel loads when chat connects`.

- [ ] 2.3 Re-login on an IRC auth refusal, read-only fallbacks — verify: uv run pytest tests/test_twitch_service.py -q passes

Covers: TSR-15, TSR-16, TSR-17, TSR-18, TSR-19 · Test: scenario-first

Files: modify src/chattersyn/services/twitch/service.py; test tests/test_twitch_service.py

Interfaces:
- Consumes: `ensure_valid(rejected_token=...)` (1.1), `on_connected` (2.1).
- `_on_auth(event)` no longer emits `auth_state`; on a signed-in connection it starts `_relogin(rejected_token)` unless one is running (single flight). Every connection-level auth NOTICE (`NOTICE_AUTH_FAILED`) takes this path.
- `_relogin_spent: bool` — set when a re-login's refresh succeeded and the connection was rebuilt; cleared by the next signed-in 001. A refusal while it is set → `_fall_back_read_only(REJECTED_MESSAGE, delete=False)` with no refresh.
- `_relogin`: tokens → set `self.tokens`, `helix.invalidate_token()` if Helix exists, rebuild IRC signed in (stop the old client first; one live client), re-join desired channels, set `_relogin_spent`. None → `_fall_back_read_only(EXPIRED_MESSAGE, delete=True)` (ensure_valid already deleted the login). `TwitchUnreachable` → emit "unreachable" and leave the IRC client running: its own backoff reconnects, Twitch refuses again, and the next refusal retries the refresh (no schedule of its own).
- Produces: `_fall_back_read_only(message: str, *, delete: bool)` — `auth.clear()` only when `delete`, `self.tokens = None`, close Helix, rebuild anonymous IRC, re-join desired channels, `bridge.auth_state.emit("twitch", False, message)`. Also used by 2.2 and 2.4.

1. Write tests (fakeirc server sending `:tmi.twitch.tv NOTICE * :Login authentication failed` then closing): `test_expired_token_relogin_rejoins` (TSR-15), `test_relogin_replaces_connection_once` (Review Focus 2), `test_relogin_single_flight` (two NOTICEs → one refresh POST), `test_new_token_refused_goes_read_only_keeps_login` (TSR-16: store still holds the login, anonymous IRC, REJECTED_MESSAGE, one refresh only), `test_revoked_login_goes_read_only` (TSR-17: refresh 400 → deleted, EXPIRED_MESSAGE), `test_refresh_unreachable_retries_at_next_refusal` (TSR-18: refresh 503, "unreachable" emitted, login kept; the next refusal POSTs again), `test_later_expiry_refreshes_again` (TSR-19: refusal → refresh → welcome → refusal → second refresh). Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `fix(twitch): re-login when chat refuses an expired token`.

- [ ] 2.4 Account features during an outage — verify: uv run pytest tests/test_twitch_service.py -q passes

Covers: TSR-20, TSR-4 · Test: scenario-first

Files: modify src/chattersyn/services/twitch/service.py; test tests/test_twitch_service.py

Interfaces:
- Consumes: `TwitchUnreachable` (1.1), `_fall_back_read_only` (2.3).
- `_user_token()`: `TwitchUnreachable` propagates (Helix's action fails with it, `self.tokens` unchanged); None → `_fall_back_read_only(EXPIRED_MESSAGE, delete=True)` scheduled once, then return None as today.
- `reconnect()` (after "Connect Twitch"): `TwitchUnreachable` is handled like today's None (keep the current connection, emit today's "sign-in succeeded but the token refresh failed — try connecting again", return False).

1. Write tests: `test_ban_during_outage_fails_only_that_action` (TSR-20: token needs a refresh, refresh 503 → `helix.ban(...)` raises, store kept; the next Helix call POSTs to TOKEN_URL again), `test_helix_definite_rejection_falls_back` (Review Focus 4 / TSR-4: refresh 400 during a Helix call → read-only IRC, EXPIRED_MESSAGE, store deleted), `test_reconnect_unreachable_keeps_connection`. Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `fix(twitch): account actions fail alone while Twitch is unreachable`.

## 3. Main window

- [ ] 3.1 Render reachability and rejections — verify: uv run pytest tests/test_main_window_twitch_signin.py -q passes

Covers: TSR-10, TSR-11, TSR-25 · Test: scenario-first

Files: modify src/chattersyn/ui/main_window.py; test tests/test_main_window_twitch_signin.py (new; same fixtures as tests/test_main_window_quota.py)

Interfaces:
- Consumes: `bridge.connection_state` events and `TwitchService.unreachable` (2.1), `bridge.auth_state("twitch", False, EXPIRED_MESSAGE | REJECTED_MESSAGE)` (2.3).
- MainWindow connects `bridge.connection_state` to `_on_connection_state(info: dict)` (Twitch only): "unreachable" → status bar "Twitch: can't reach Twitch — retrying…" and `_twitch_retrying = True`, no tab rows, account row untouched; "connected" with detail "signed_in" → if `_twitch_retrying`: status bar "Twitch connected"; clear `_twitch_retrying` and `_twitch_auth_problem`.
- `_on_auth_state("twitch", False, detail)`: clears `_twitch_retrying`, stores `_twitch_auth_problem = detail`; the status bar and the one row per tab show `detail` without the "twitch: " prefix when `detail` starts with "Twitch" (the new texts), with it otherwise (today's texts).
- `_open_settings`: Twitch row "Not connected — <_twitch_auth_problem>" while it is set and the service holds no tokens.
- `_start_services`: shows today's launch status only when `self._twitch.unreachable` is False; the "Twitch unavailable: …" rows stay for unexpected exceptions only.

1. Write tests: `test_launch_unreachable_shows_retrying_without_rows` (TSR-10 + Review Focus 5: fake service whose `start()` emits "unreachable" and sets `unreachable`; status bar text exact, no tab row added, Settings row "Connected as <login>"), `test_signed_in_connect_after_retrying_shows_connected` (TSR-11), `test_connect_without_retrying_keeps_status`, `test_rejection_outranks_retrying` (TSR-25: "unreachable", then auth_state False EXPIRED_MESSAGE, then "connected"/"anonymous" → status bar shows the expired message, never "Twitch connected"; each tab got one row with the message), `test_settings_row_shows_problem_until_signed_in_connect`. Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run ruff check src/ tests/` and `uv run pytest -q`. Expected: PASS.
4. Commit `fix(ui): Twitch status shows retrying and sign-in problems`.
