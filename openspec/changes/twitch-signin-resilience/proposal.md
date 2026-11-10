## Why

The audit of 2026-11-08 found two ways ChatterSyn loses Twitch for no good reason.
A token refresh that fails for any reason (a Twitch server error, a timeout, no
network) deletes the saved login, so a short outage signs the user out. And a
launch without network makes the token check throw before the chat connection is
created, so Twitch stays dead until the app restarts. A third hole sits next to
them: chat keeps reconnecting with an expired token, and the "Login authentication
failed" answer is only logged.

## What Changes

- The saved Twitch login is deleted only when Twitch definitively rejects it.
- A launch without network connects chat with the saved login and catches up the
  token check, account features, badges and emotes once chat connects.
- The launch token check gives up after 10 seconds and connects anyway.
- An expired token on the chat connection is refreshed once and the connection
  retried; a revoked or still-rejected login falls back to read-only chat with a
  message.
- While Twitch can't be reached, the status bar says so instead of a system row in
  every tab.

## Capabilities

### New Capabilities
- `twitch-signin`: keeping the Twitch sign-in and chat connection alive across
  outages, offline launches and expired tokens.

### Modified Capabilities

## Impact

The Twitch sign-in (token check and refresh), the Twitch service start and
reconnect logic, the IRC connection's handling of a rejected login, and the main
window's Twitch status messages. No new settings, permissions or dependencies.
