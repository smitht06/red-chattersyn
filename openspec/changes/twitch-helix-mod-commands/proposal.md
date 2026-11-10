## Why

Twitch stopped accepting chat commands over IRC in February 2023. ChatterSyn moved
/ban, /timeout and /unban to Helix in v0.6.0, but 31 other moderation and
broadcaster commands (/slow, /vip, /raid, /announce, …) are still sent as chat
lines and silently do nothing. Six more (/delete, /mods, /vips, /settitle,
/setgame, /uptime) are "not implemented yet" stubs, and /host and /unhost refer to a
feature Twitch retired in 2022.

## What Changes

- The 31 dead commands and /delete, /mods, /vips, /settitle, /setgame, /uptime run
  through Twitch's Helix API.
- Sign-in asks for 11 new Twitch permissions; users reconnect once. A command whose
  permission is missing asks the user to reconnect instead of sending.
- Each success shows a short confirmation row unless Twitch's own chat event shows
  the result; failures show Twitch's reason.
- /mods and /vips open a filterable list popup; clicking a name opens the user card.
- The Twitch user card gets a "Delete message" button for mods.
- /host and /unhost explain that hosting was retired and point to /raid.

## Capabilities

### New Capabilities
- `twitch-mod-commands`: Twitch moderation and broadcaster chat commands run through
  Helix, their permissions, feedback, and the mods/VIPs list.

### Modified Capabilities

## Impact

The command engine, the Helix client, the Twitch sign-in scopes, MainWindow's
command runner, the user card, and a new list popup. Users reconnect Twitch once.
