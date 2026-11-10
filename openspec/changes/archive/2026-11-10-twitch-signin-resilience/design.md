# Design: twitch-signin-resilience

## Context (facts checked 2026-11-09)

- `TwitchAuth.refresh` returns None for every non-200 answer and lets network
  errors propagate; `ensure_valid` deletes the saved login whenever refresh
  returns None, so a 5xx or 429 signs the user out (audit #2).
- `ensure_valid` calls `validate`, which raises on network errors and on 5xx
  (`raise_for_status`). `TwitchService.start` awaits it before creating the IRC
  client, so an offline launch never creates IRC; MainWindow then writes
  "Twitch unavailable: <error>" into every tab (audit #3).
- The auth HTTP client allows 10 s to connect and 20 s to read, per request.
- `TwitchIrc` keeps the token it was built with. A connection-level
  "Login authentication failed" NOTICE becomes `AuthFailed`, which the service
  only logs, while IRC keeps reconnecting with the same token on its backoff.
- The saved login (`TwitchTokens`) stores `login` and `user_id`, so a signed-in
  IRC connection can be built without asking Twitch first.
- Helix pulls its token through `ensure_valid` on every call; it is only created
  once a validated token exists. Channel badges/emotes load once per join and are
  never retried.

## Goals / Non-Goals

Goals:

- A Twitch outage or a missing network never deletes the saved login (D2).
- Launching without network still ends in a signed-in chat once the network is
  back, with account features, badges and emotes caught up, without a restart
  (D3, D9, D12).
- An expired token on the chat connection is replaced without user action (D1).
- A login Twitch really rejects ends in read-only chat with a clear message,
  never in a silent reconnect loop (D5, D7).

Non-Goals:

- The other audit findings (Helix /users/follows, USERSTATE message ids, IRC
  moderation commands).
- Refreshing the token ahead of its expiry time.
- Reading chat anonymously while the token can't be refreshed (D8).

## Open questions

None.

## Decision log

<!-- grillspec: depth=standard council=no budget=15 asked=12 -->

### Open


### Settled

- [x] D1 Scope — **#2 + #3 + IRC re-login on an expired token**: on "Login authentication failed" refresh the token and reconnect · accepted · alternatives: #2 + #3 only (long sessions keep a silent dead-token reconnect loop) · reversible: cheap
- [x] D2 Deleting the saved login — **only on a definite rejection**: refresh answered 400/401 (invalid refresh token) or no refresh token exists; 5xx, 429, timeouts and network errors keep the login and retry later · accepted · alternatives: never delete automatically (revoked login lingers, every launch retries) · reversible: cheap
- [x] D3 Offline launch with a saved login — **connect IRC with the saved token right away**, no validation first; IRC retries until the network is back, and an expired token goes through the D1 re-login. The saved login carries the username, so sending works once connected · accepted · alternatives: anonymous read-only first, then switch (reconnect swap, can't send meanwhile) · reversible: cheap
- [x] D4 While Twitch can't be reached — **status bar only**: "Twitch: can't reach Twitch — retrying…"; the account stays shown as connected; no per-tab system rows (replaces today's "Twitch unavailable: <error>" row in every tab) · accepted · alternatives: status bar + one row per tab (noise on every blip) · reversible: cheap
- [x] D5 Mid-session re-login definitively rejected (revoked) — **fall back to read-only**: delete the login (D2), reconnect anonymously so chat keeps flowing, show "Twitch sign-in expired — reconnect your account" in the status bar and the Settings account row; same end state as signing out · accepted · alternatives: stop Twitch chat until reconnect · reversible: cheap
- [~] D6 Catch-up after an offline launch — **when chat connects**: the first successful IRC connect triggers the token check, starts account features (Helix) and reloads badges/emotes for channels whose load failed; no extra timer (today they never come back without a restart) · accepted · alternatives: background retry timer 30 s–5 min (second retry loop) · reversible: cheap · superseded by D12
- [x] D7 Loop guard — **one refresh per rejection, then read-only**: if IRC rejects the freshly refreshed token too, reconnect anonymously and show "Twitch rejected the sign-in — reconnect your account"; the saved login is KEPT (Twitch did not revoke it) · accepted · alternatives: same as revoked (deletes a login the API still accepts) · reversible: cheap
- [x] D8 Re-login refresh can't reach Twitch — **keep retrying signed-in** on IRC's normal reconnect backoff, keep the login (D2), status "Twitch: can't reach Twitch — retrying…" (D4); chat is down until the refresh works · accepted · alternatives: read anonymously meanwhile (two connection swaps) · reversible: cheap
- [x] D9 Normal launch — **keep the check/refresh before IRC connects, 10 s overall limit**: if Twitch hasn't answered within 10 s (or errors per D2), take the D3 path (connect with the saved token) · accepted · alternatives: 5 s limit; skip the check and always connect first (expired token costs one IRC reject) · reversible: cheap
- [x] D10 Mid-session account-feature (Helix) refresh that can't reach Twitch — **keep the tokens, that one call fails** and the next call retries (follows D2) · agent's call · alternatives: none that fit D2 · reversible: cheap
- [x] D11 Status after recovery — **"Twitch connected" in the status bar** once chat connects after the unreachable state (replaces the "retrying…" text) · agent's call · alternatives: no message · reversible: cheap
- [x] D12 Catch-up — **on every chat connect, retry whatever is still missing**: a pending token check, account features (Helix), and badges/emotes of each channel whose load failed (per channel, including channels opened during an outage and the 10-s launch path); nothing is reloaded when nothing is missing; still no timer of its own · accepted · alternatives: offline launch only (D6 as written; channels opened mid-outage stay without badges/emotes) · reversible: cheap
- [x] D13 Launch with a definitively rejected login — **same text as revoked mid-session**: "Twitch sign-in expired — reconnect your account" in the status bar and the Settings account row, then read-only chat (replaces "stored tokens invalid — reconnect account") · accepted · alternatives: keep today's text · reversible: cheap
- [x] D14 Builder-check fill-ins (no product effect): read-only = the anonymous connection signed-out mode already uses; a launch check abandoned at 10 s is cancelled and the catch-up (D12) checks again; the one-refresh guard (D7) is spent only by a refresh that succeeded and is cleared by the next signed-in connect; "can't reach Twitch" is set when a chat connect attempt or a token refresh can't reach Twitch and cleared when chat connects; a failed account action shows the error it shows today · agent's call · reversible: cheap
- [x] D15 Real sign-in rejection (D5, D7, D13) — **also one system row per tab**, as today ("Twitch sign-in expired — reconnect your account" / "Twitch rejected the sign-in — reconnect your account"); network blips stay status-bar-only (D4) · accepted · alternatives: status bar + Settings only (easy to miss) · reversible: cheap
- [x] D16 Re-check fill-ins (no product effect, follow from D2/D7/D9/D12): the Settings row reads "Not connected — <message>" after a rejection and returns to "Connected as <login>" on the next signed-in connect; an expired/rejected message replaces the "retrying…" status and a following read-only connect does not show "Twitch connected"; a login kept after D7 is tried again at the next launch (same flow) and the Settings Connect button starts a fresh sign-in; token checks and refreshes run one at a time (catch-up, launch and re-login share one in-flight refresh); D8's retry has no schedule of its own: each IRC reconnect attempt re-runs the refresh; "failed" channel metadata = the account-feature load (user lookup, badges, channel emotes) errored or could not run, and the whole channel load is redone; catch-up runs on signed-in connects only (read-only connects keep today's per-join emote load); the live-status poll fails silently and retries on its normal poll; sending before chat connects behaves as today; a valid token with no refresh token is kept until it expires · agent's call · reversible: cheap
