## Why

Signed-in Twitch user cards never show the avatar, account age or follower count.
The card also asks Twitch whether you follow the user, through `/helix/users/follows`,
an endpoint Twitch removed on 2023-09-06; that call fails and throws away every
other detail the card had loaded. The row it fills is labelled "Follows you since:"
but computed the other way round (whether you follow them).

## What Changes

- Each user-card detail loads on its own; one failing lookup hides only its own row.
- The follow row becomes "Followed <channel> since: <date>": when the card's user
  followed the channel of the tab the card was opened from, shown to that channel's
  broadcaster and mods.
- ChatterSyn asks Twitch for the `moderator:read:followers` permission; users who
  signed in before must reconnect Twitch once to see the follow row.

## Capabilities

### New Capabilities
- `twitch-usercard`: the details a Twitch user card shows and where they come from.

### Modified Capabilities

## Impact

The Twitch sign-in scopes, the Helix client's follow lookup, and the main window's
user-card enrichment. Users reconnect Twitch once for the follow row.
