# twitch-signin Specification

## Purpose

Keeps the Twitch sign-in and the Twitch chat connection alive across Twitch
outages, launches without network and expired tokens, and deletes the saved login
only when Twitch really rejects it.

Terms:

- The **saved login** is the Twitch sign-in stored in the OS keyring.
- **Unreachable** means Twitch did not answer, answered with a server error (5xx)
  or a rate limit (429), or the network is down.
- A **definite rejection** is a token refresh answered with 400 or 401, or an
  expired token with no refresh token.
- **Read-only** is the anonymous chat connection that signed-out mode uses today:
  chat is read, the account row shows Twitch as not connected, and sending to
  Twitch is refused as it is when signed out.
- The **account row** is the Twitch status in Settings → Accounts: "Connected as
  <login>" while signed in, "Not connected" otherwise.
- **Announcing** a sign-in message means: the status bar shows it, every tab gets
  one system row with it, and the account row reads "Not connected — <message>"
  until chat next connects signed in.
- A **channel load** is a channel's user lookup, badges, and Twitch and
  third-party emotes; it has **failed** when its account-feature part errored or
  could not run.
- The **expired message** is "Twitch sign-in expired — reconnect your account";
  the **rejected message** is "Twitch rejected the sign-in — reconnect your
  account".

## Requirements

### Requirement: The saved login survives outages
ChatterSyn SHALL delete the saved Twitch login only on a definite rejection. When
Twitch is unreachable during a token check or refresh, ChatterSyn SHALL keep the
saved login, keep the account row showing Twitch as connected, and try again later.
A token that still works but has no refresh token SHALL be kept until it expires.
Token checks and refreshes SHALL run one at a time: callers that need a refresh
while one is in flight SHALL share its result.

#### Scenario: TSR-1 refresh hits a Twitch server error
- **WHEN** the saved token has expired and the refresh is answered with a 5xx
  error
- **THEN** the saved login is kept and the account row still shows Twitch as
  connected

#### Scenario: TSR-2 refresh without network
- **WHEN** the saved token has expired and the refresh fails because the network
  is down or the request times out
- **THEN** the saved login is kept

#### Scenario: TSR-3 refresh is rate-limited
- **WHEN** the refresh is answered with 429
- **THEN** the saved login is kept

#### Scenario: TSR-4 refresh token rejected
- **WHEN** the refresh is answered with 400 or 401
- **THEN** the saved login is deleted, Twitch chat is read-only, and the expired
  message is announced

#### Scenario: TSR-5 expired token with no refresh token
- **WHEN** the saved token has expired and the saved login holds no refresh token
- **THEN** the saved login is deleted, Twitch chat is read-only, and the expired
  message is announced

#### Scenario: TSR-23 working token with no refresh token
- **WHEN** the saved token still works and the saved login holds no refresh token
- **THEN** the saved login is kept and chat connects signed in

#### Scenario: TSR-24 one refresh at a time
- **WHEN** two parts of the app need a token refresh at the same moment
- **THEN** one refresh request is sent to Twitch and both use its result

### Requirement: Launch with a saved login
At launch with a saved login, ChatterSyn SHALL check the token, refreshing it if it
has expired, and connect Twitch chat with the resulting token. When Twitch is
unreachable, or the check has not finished within 10 seconds, ChatterSyn SHALL
cancel the check and connect Twitch chat with the saved login as stored; the chat
connection SHALL keep retrying until it connects, signed in as the saved account,
without a restart. A definite rejection at launch SHALL end in read-only chat as in
TSR-4.

#### Scenario: TSR-6 launch with an expired token
- **WHEN** the app launches with the network up and the saved token has expired
- **THEN** the token is refreshed before chat connects, and chat connects signed in
  with the new token

#### Scenario: TSR-7 launch without network
- **WHEN** the app launches with a saved login while the network is down, and the
  network comes back later
- **THEN** Twitch chat connects signed in as the saved account without a restart,
  and the user can send messages

#### Scenario: TSR-8 token check is slow
- **WHEN** the launch token check has not finished after 10 seconds
- **THEN** the check is cancelled and chat starts connecting with the saved login

#### Scenario: TSR-9 Twitch server error at launch
- **WHEN** the launch token check is answered with a 5xx error
- **THEN** chat connects with the saved login and the saved login is kept

#### Scenario: TSR-21 login rejected at launch
- **WHEN** the app launches and the refresh of the saved login is answered with
  400
- **THEN** the saved login is deleted, chat connects read-only, and the expired
  message is announced

### Requirement: Twitch status while unreachable
When a chat connect attempt or a token refresh can't reach Twitch, ChatterSyn SHALL
show "Twitch: can't reach Twitch — retrying…" in the status bar, leave the account
row unchanged, and add no system rows to the tabs. When chat next connects signed
in, ChatterSyn SHALL replace that text with "Twitch connected". An announced
expired or rejected message SHALL replace the "retrying…" text, and the read-only
connect that follows SHALL NOT show "Twitch connected".

#### Scenario: TSR-10 launch without network shows one status
- **WHEN** the app launches with a saved login while the network is down
- **THEN** the status bar shows "Twitch: can't reach Twitch — retrying…", the
  account row shows Twitch as connected, and no tab gets a "Twitch unavailable" row

#### Scenario: TSR-11 recovery is announced
- **WHEN** chat connects signed in while the status bar shows "Twitch: can't reach
  Twitch — retrying…"
- **THEN** the status bar shows "Twitch connected"

#### Scenario: TSR-25 rejection outranks the retry status
- **WHEN** the status bar shows "Twitch: can't reach Twitch — retrying…" and the
  refresh then gets a definite rejection
- **THEN** the expired message is announced and the read-only connect that follows
  does not show "Twitch connected"

### Requirement: Catch-up when chat connects
Every time Twitch chat connects signed in, ChatterSyn SHALL retry what is still
missing: a token check not yet successful since launch, account features (user
cards, bans and timeouts, live status) that could not start, and each failed
channel load, in full, including channels opened while Twitch was unreachable. It
SHALL redo nothing that loaded and use no retry timer of its own. Read-only
connects keep today's emote loading.

#### Scenario: TSR-12 account features after an offline launch
- **WHEN** the app launched without network and chat has since connected
- **THEN** the token has been checked, a user card opened afterwards shows the
  account details, and the next live-status poll reports the channel's status

#### Scenario: TSR-13 badges and emotes after an offline launch
- **WHEN** a channel's badges and emotes failed to load at launch and chat has
  since connected
- **THEN** that channel's badges and emotes are loaded again

#### Scenario: TSR-14 channels that loaded are left alone
- **WHEN** chat connects again and a channel's badges and emotes had loaded
- **THEN** that channel's badges and emotes are not loaded again

#### Scenario: TSR-22 channel opened during an outage
- **WHEN** the user opens a channel mid-session while Twitch is unreachable, and
  chat connects later
- **THEN** that channel's badges and emotes are loaded

### Requirement: Expired token on the chat connection
When the Twitch chat connection is refused with "Login authentication failed",
ChatterSyn SHALL refresh the token and reconnect with the new token, re-joining
every channel. At most one successful refresh SHALL be spent per rejection: the
allowance is restored when chat next connects signed in. If Twitch is unreachable
for the refresh, ChatterSyn SHALL stay signed in and run the refresh again at each
of the chat connection's existing reconnect attempts, with no schedule of its own.

#### Scenario: TSR-15 expired token is replaced
- **WHEN** the chat connection is refused with "Login authentication failed" and
  the refresh succeeds
- **THEN** chat reconnects signed in with the new token and every channel is joined
  again

#### Scenario: TSR-18 refresh can't reach Twitch
- **WHEN** the chat connection is refused and Twitch is unreachable for the refresh
- **THEN** the saved login is kept, the status bar shows "Twitch: can't reach
  Twitch — retrying…", and the refresh is tried again at the next reconnect

#### Scenario: TSR-19 a later expiry gets its own refresh
- **WHEN** a refresh replaced an expired token, chat then connected signed in, and
  later the chat connection is refused again
- **THEN** the token is refreshed again

### Requirement: Rejected sign-in falls back to read-only
When the chat connection's refresh is a definite rejection, ChatterSyn SHALL delete
the saved login, connect read-only and announce the expired message. When the chat
connection refuses the freshly refreshed token before connecting signed in,
ChatterSyn SHALL connect read-only, keep the saved login and announce the rejected
message; the kept login SHALL be tried again at the next launch.

#### Scenario: TSR-16 new token refused too
- **WHEN** the chat connection refuses the token that the refresh just returned
- **THEN** chat reconnects read-only, the saved login is kept, no further refresh
  is attempted, and the rejected message is announced

#### Scenario: TSR-17 login revoked
- **WHEN** the chat connection is refused and the refresh is a definite rejection
- **THEN** the saved login is deleted, chat reconnects read-only, and the expired
  message is announced

### Requirement: Account features during an outage
When an account feature (a user card, a ban or timeout, the live status) needs a
token refresh and Twitch is unreachable, ChatterSyn SHALL fail only that action,
showing the error that action shows today (the live-status poll fails silently and
tries again at its next poll), and keep the saved login; the next action SHALL try
the refresh again.

#### Scenario: TSR-20 ban during an outage
- **WHEN** the user bans someone while the token needs a refresh and Twitch is
  unreachable
- **THEN** the ban fails with its usual error, the saved login is kept, and the
  next action tries the refresh again
