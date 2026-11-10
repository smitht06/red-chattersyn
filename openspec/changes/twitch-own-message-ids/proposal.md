## Why

Twitch never sends a user's own chat messages back to them; it answers each one
with a USERSTATE line carrying the message's real id. ChatterSyn ignores that id,
so its own rows keep a placeholder id: a mod's delete or a timeout doesn't grey
them out, /delete and right-click delete send the wrong id, and replies to them
point at an id Twitch doesn't know (audit finding #5).

## What Changes

- Each message the user sends takes its real id from Twitch's USERSTATE answer,
  matched in send order per channel; a send Twitch refuses consumes the oldest
  waiting send.
- The dead "echo" code that waited for Twitch to send the message back is removed.

## Capabilities

### New Capabilities
- `twitch-own-messages`: the ids of messages the user sends on Twitch.

### Modified Capabilities

## Impact

The Twitch chat connection (`services/twitch/irc.py`) and its tests. No UI, setting
or permission changes.
