# Design: twitch-helix-mod-commands

## Context (facts checked 2026-11-10)

- `services/commands.py`: `_IRC_MOD_COMMANDS` (35 names) validate their arguments
  in `_cmd_irc_mod`; ban/banid/timeout/unban/untimeout become a `ModAction` that
  MainWindow runs through Helix (`_twitch_mod`); the other 31 are sent as
  `/<name> <args>` PRIVMSG, which Twitch ignores since 2023. `_MOD_STUB_COMMANDS`
  answer "The /<name> command is not implemented yet."; `_PASSTHROUGH_COMMANDS`
  sends /help /disconnect /host /unhost to Twitch.
- `_twitch_mod` checks `MODERATION_SCOPE in tokens.scopes` and writes "Reconnect your
  Twitch account (Connect Twitch Account…) to grant ChatterSyn moderation
  permission." when it is missing; failures become "Twitch <verb> failed: <reason>".
- Helix endpoints and scopes (Twitch API reference): warnings
  `POST /moderation/warnings` (moderator:manage:warnings); announcements
  `POST /chat/announcements` (moderator:manage:announcements); chat settings
  `PATCH /chat/settings` (moderator:manage:chat_settings); shoutouts
  `POST /chat/shoutouts` (moderator:manage:shoutouts); shield mode
  `PUT /moderation/shield_mode` (moderator:manage:shield_mode); delete/clear
  `DELETE /moderation/chat` (moderator:manage:chat_messages); moderators
  `POST/DELETE/GET /moderation/moderators` (channel:manage:moderators); VIPs
  `POST/DELETE/GET /channels/vips` (channel:manage:vips); raids `POST/DELETE /raids`
  (channel:manage:raids); commercials `POST /channels/commercial`
  (channel:edit:commercial); markers `POST /streams/markers` and title/category
  `PATCH /channels` (channel:manage:broadcast); categories `GET /search/categories`;
  uptime `GET /streams` (no scope).
- `UsercardPopup` builds Ban/Unban/Timeout buttons for Twitch cards when the user has
  mod rights; custom actions already run through the same command dispatch.
  `MentionsPopup(BasePopup)` is the existing list-popup pattern.

## Goals / Non-Goals

Goals: every Twitch moderation/broadcaster command ChatterSyn lists works again
(D1, D2); missing permissions and failures are explained (D3, D5); mods can list
mods/VIPs and delete a message from the user card (D6, D9, D10, D11).

Non-Goals: the remaining stubs (/clip, /poll, /prediction, /pin, /monitor, …);
YouTube moderation; a reconnect prompt beyond the per-command row.

## Open questions

None.

## Decision log

<!-- grillspec: depth=standard council=no budget=15 asked=11 -->

### Open


### Settled

- [x] D1 Commands — **the 31 dead IRC commands plus the cheap stubs /delete, /settitle, /setgame, /uptime, /mods, /vips**, all through Helix; the other stubs stay stubs · user · alternatives: the 31 only; mod-used subset only · reversible: cheap
- [x] D4 /host, /unhost — **explain and point to /raid**: a row "Twitch retired hosting — use /raid <channel> instead."; no request is sent · accepted · alternatives: remove them (unknown command) · reversible: cheap
- [x] D2 Permissions — **all 11 new scopes at once, one reconnect**: moderator:manage:warnings, moderator:manage:announcements, moderator:manage:chat_settings, moderator:manage:shoutouts, moderator:manage:shield_mode, moderator:manage:chat_messages, channel:manage:moderators, channel:manage:vips, channel:manage:raids, channel:edit:commercial, channel:manage:broadcast · accepted · alternatives: mod-level scopes only (broadcaster commands stay unusable) · reversible: costly (users re-consent)
- [x] D3 Missing permission — **nothing is sent; a row asks to reconnect**: "Reconnect your Twitch account (Connect Twitch Account…) to grant ChatterSyn permission for /<command>." (same pattern as today's /ban check) · accepted · alternatives: send and show Twitch's error (cryptic) · reversible: cheap
- [x] D5 Success feedback — **a short confirmation row, unless Twitch's own chat event shows the result** (announcements appear in chat; /clear and /delete clear it); failures always show Twitch's reason · accepted · alternatives: failures only · reversible: cheap
- [x] D6 /mods and /vips — **open a list panel** (new UI; no panel exists today) instead of a /chatters-style row · user · alternatives: one row like /chatters · reversible: cheap
- [x] D8 /setgame — **exact category name (ignoring case), else Twitch's top search result**, and the confirmation row names the category set ("Category set to Grand Theft Auto V.") · accepted · alternatives: exact match only · reversible: cheap
- [x] D9 /delete — **/delete <message-id>, plus a "Delete message" button on the user card** · user · alternatives: /delete only (custom actions can pass {msg.id}) · reversible: cheap
- [x] D10 Mods/VIPs panel — **popup like /mentions with a name filter**: title "Moderators (N)" / "VIPs (N)", names A–Z, click opens that user's card, Esc closes · accepted · alternatives: plain list · reversible: cheap
- [x] D11 Card "Delete message" — **shown only on Twitch cards opened from that user's own chat message, when you have mod rights there; deletes at once, no confirm** (not on @mention or /usercard cards) · accepted · alternatives: ask first · reversible: cheap
- [x] D12 **One change** · accepted · alternatives: split commands / panel+button · reversible: cheap
- [x] D13 Fill-ins (no product effect, follow existing behaviour): argument defaults keep Twitch's old IRC ones (/slow 30 s, /followers 0 min, /commercial 30 s); /uptime replies "<channel> has been live for 2h 14m" or "<channel> is offline"; /settitle needs text; confirmation rows use plain sentences ("Slow mode on (30 s).", "You added bob as a VIP.", "Raiding carol."); every command works from typed input and from user-card custom actions (same dispatch) · agent's call · reversible: cheap
- [x] D14 Spec fill-ins (follow existing texts): not signed in → today's "Connect a Twitch account to moderate."; failures → "Twitch /<command> failed: <reason>" (today's "Twitch <verb> failed: …" pattern); /setgame with no search hit → "No Twitch category matches <name>." and nothing changes · agent's call · reversible: cheap
- [x] D15 Builder-check fill-ins (facts + existing behaviour): argument checks and usage rows stay today's `_IRC_MOD_SIG`/`_IRC_MOD_USAGE`, plus "Usage: /settitle <title>", "Usage: /setgame <category>", "Usage: /delete <message-id>"; /followers takes a duration like /timeout (bare number = minutes, as on IRC) sent as minutes; the other stubs, /help and /disconnect are unchanged; an already-in-state target (already a VIP…) shows Twitch's refusal as the failure row; /uptime and /host /unhost need no permission; Twitch lets only the broadcaster list moderators and VIPs, so a mod's /mods or /vips shows Twitch's refusal; lists fetch every page; an empty list opens with "No moderators." / "No VIPs."; /uptime durations read "2h 14m", or "14m" under an hour; a failed delete (already gone, a mod's message) shows the failure row; the card's mod rights are the same ones that show Ban/Timeout; the hosting row shows the literal "<channel>"; confirmation texts are listed in the spec · agent's call · reversible: cheap
