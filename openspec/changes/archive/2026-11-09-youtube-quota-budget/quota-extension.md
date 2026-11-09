# ChatterSyn: draft answers for the YouTube quota extension

Draft for the developer to paste into Google's "YouTube API Services - Audit and
Quota Extension Form". Nothing here has been submitted. Fill in every
`[PLACEHOLDER]` before sending. Re-check the form's own wording: the question
headings below are grouped by topic, not copied from the form.

No secrets belong in this file or in the form: no OAuth client secret, no API
keys, no tokens.

## Placeholders only the developer can fill in

| Item | Value |
| --- | --- |
| Google Cloud project number | `[PLACEHOLDER: project number]` |
| OAuth client ID (public, as in the app) | `[PLACEHOLDER: copy from services/youtube/auth.py BUILTIN_CLIENT_ID]` |
| Developer / company name | `[PLACEHOLDER]` |
| Contact name, email, phone | `[PLACEHOLDER]` |
| Screencast / demo video link | `[PLACEHOLDER: URL; show sign-in, the Stream tab, a channel tab, a live chat, sending a message, the quota-wait message]` |
| Expected number of daily active users | `[PLACEHOLDER: number]` |
| Quota being requested (units/day) | `[PLACEHOLDER: expected users x per-user cost below, plus headroom]` |
| Current peak daily usage seen in Cloud Console | `[PLACEHOLDER: from IAM & Admin > Quotas, YouTube Data API v3]` |
| Measured streamList reconnect rate | `[PLACEHOLDER: see "Assumptions", measure on a real 2-hour stream]` |

## What ChatterSyn is

ChatterSyn is a free, open-source desktop chat client (Python and Qt, for macOS,
Windows and Linux). It shows Twitch and YouTube live chat in one window and has
one input box that sends a message to both chats. Source:
https://github.com/smitht06/red-chattersyn, site: https://chattersyn.redcyberdragon.net.

There is no ChatterSyn server that sees user data. Each user's copy of the app
talks to YouTube directly from their own computer. The only server is a small
sign-in relay that exchanges and renews Google tokens and stores nothing (see
the privacy policy at https://chattersyn.redcyberdragon.net/privacy.html).

Every signed-in user of the app uses the same Google Cloud project's quota
(10,000 units/day by default), because the app ships one built-in OAuth client.
A user can optionally enter their own API key. Their public reads then count
against the key's own project, not ours.

## Why the app uses the YouTube Data API

- Read a live chat the user has open (a stream, a channel's current stream, or
  their own stream).
- Send the message the user types into that chat.
- Find out when a channel or the user's own broadcast goes live, so the chat tab
  attaches by itself.
- Show the stream title, viewer count and uptime above the chat.
- Show chatters' display names (the streaming chat feed carries only channel ids).
- Let a channel owner or moderator ban, time out and unban a chatter from the
  user card.

OAuth scope requested: `https://www.googleapis.com/auth/youtube.force-ssl`
(one scope; needed for liveChatMessages.insert and liveChatBans, and for reading
the user's own broadcast with `mine=true`).

## Every YouTube API method the app calls

Unit costs are from Google's quota table
(developers.google.com/youtube/v3/determine_quota_cost, checked 2026-11-08).
"Key" means the call uses the user's own API key when one is set and works
(otherwise the sign-in, which is the shared project quota). "Sign-in" means it is
always made with the user's OAuth token and counts against the shared project.

| Method | Cost | Credential | When it runs |
| --- | --- | --- | --- |
| `channels.list` (`forHandle` or `id`, part contentDetails/snippet/statistics) | 1 | Key | Once when a channel tab starts or after the quota resets (deep scan). Not on a normal re-check. Exception: a channel with no public uploads has nothing remembered, so every check is this lookup plus a 404 `playlistItems.list` (2 units). |
| `playlistItems.list` (uploads playlist) | 1 | Key | Deep scan: one call per page of 50 uploads, at most 10 pages, stops early when a live video is found. Every re-check of a channel tab: one page only. |
| `videos.list` (liveStreamingDetails, snippet; up to 50 ids) | 1 | Key | Deep scan: one call per page of 50. Re-check: one call, only when the newest page changed or a video on it is upcoming/live. Video-URL tabs: one call per check. Stats refresh while a stream is read: every 120 s. |
| `channels.list` (`part=snippet`, up to 50 ids) | 1 | Key | Display names of chatters not seen yet this session; first messages are held 0.3 s so one call covers a burst. Names are cached in memory until the app closes. |
| `channels.list` (`mine=true`) | 1 | Sign-in | Once after sign-in or reconnect, for the user's own name and channel id. |
| `liveBroadcasts.list` (`mine=true`) | 1 | Sign-in | The Stream tab (the user's own broadcast), every 2 min while no broadcast is live; every 30 s from 5 min before to 15 min after a scheduled start. |
| `liveChatMessages.streamList` (gRPC) | 5 per connection | Key, or sign-in for the Stream tab | Once per live chat read, plus one per reconnect. No per-message cost. This is the normal way chat is read. |
| `liveChatMessages.list` (REST) | 5 per poll | Sign-in | Fallback only: after 3 consecutive gRPC failures for a signed-in user. At most one poll every 12 s (up to 1,500 units/hour while it is in use). See "Known risk". |
| `liveChatMessages.insert` | 20 | Sign-in | One call per message the user sends to YouTube. |
| `liveChatBans.insert` | 200 | Sign-in | A ban or timeout the user chooses on the user card. |
| `liveChatBans.delete` | 200 | Sign-in | An unban the user chooses on the user card. |

`search.list` is not called by the app (a helper for it exists in the code but
nothing uses it). The app does not use `videos.rate`, `commentThreads`,
`subscriptions`, uploads or any write method other than the three live chat ones
above.

### How often the checks run (code: `schedule.py`, `resolver.py`, `service.py`)

A channel or video tab that is not live checks again on this schedule:

| Situation | Check interval |
| --- | --- |
| Tab on screen, nothing scheduled | Every 2 min for the first 30 min offline, then every 5 min |
| Tab on screen, a scheduled stream is known | Every 15 min until 5 min before the start, then every 30 s until 15 min after it, then back to 2 min / 5 min |
| Tab not on screen (another tab selected, or window minimized) | Every 15 min |
| Channel handle/id not found | Every 15 min, on screen or not |
| The user's own Stream tab (`mine:`) | Every 2 min, hidden or not; 30 s from 5 min before to 15 min after a scheduled start |
| Channel or video tab brought on screen | One check at once if the last check is over 60 s old (so tab switching costs at most about one check per minute). Not for the Stream tab or a not-found channel, which keep their own rhythm. |
| Live stream being read | No checks; stats refresh `videos.list` every 120 s |
| Quota used up | No calls at all until the reset (midnight Pacific plus a random 1 to 10 min), then one fresh deep scan |

A check on a channel the app already knows costs 1 unit (one `playlistItems.list`
page) when the newest uploads are unchanged, and 2 units when a video on the page
is upcoming or live (it also reads `videos.list`). The first check of a channel,
and the first after a quota reset, is a deep scan: 3 units for a channel with up
to 50 uploads, at most 21 units (1 + 10 pages x 2) for a long catalogue.

Before this change an idle channel tab polled every 60 s and rescanned the whole
uploads list each time: 4,320 to 30,240 units per tab per day while offline
(1,440 checks x 3 to 21 units), and up to 60,480 while a stream was scheduled
(checked every 30 s). That is why the shared project quota ran out.

## Per-user daily cost

The figures are computed from the intervals above and from the unit costs in the
table. The idle-tab figures were also reproduced by the project's tests, which
run the real resolver and schedule for 24 simulated hours against a fake API that
counts calls (`test_idle_channel_tab_day_budget` in `tests/test_youtube_service.py`).

### Worst case: tabs left open and idle for 24 hours

| Tab left open 24 h | Units/day |
| --- | --- |
| One channel tab on screen, typical channel | about 300 (15 checks in the first 30 min + 282 at 5 min = 297, plus a 3-unit scan) |
| Same, every re-check also reads `videos.list` (a video upcoming without a start time) | 597 |
| Same, channel with no public uploads (every check is a lookup plus a 404) | 596 |
| One channel tab not on screen | about 99 (96 checks plus the scan), at most 195 |
| A channel tab whose handle is not found | 96 |
| The user's own Stream tab, no broadcast (checked every 2 min, on screen or not) | 720 |
| **Combined worst case: Stream tab + one on-screen channel tab** | **720 + about 300 = about 1,020, at most 720 + 597 = 1,317** |
| Each further (hidden) channel tab | about 99 each, at most 195 |

Only one tab is on screen at a time, so there is at most one on-screen channel
tab. The bound of 600 units/day (597 and 596 above) is per on-screen channel tab
and is consistent with the test's <= 600 limit. The Stream tab is checked every
2 minutes whether it is on screen or not, so it adds its 720 on top, and every
hidden channel tab adds about 99 to 195. A user with a Stream tab, one on-screen
channel tab and three hidden channel tabs would spend about 720 + 300 + 3 x 99 =
1,317 units on an idle day, at most 720 + 597 + 3 x 195 = 1,902.

### Typical day for a user who streams: one Stream tab and two channel tabs, app open 4 hours, one 2-hour stream

Assumptions (change them if your usage data says otherwise):

- No personal API key is set, so every call counts against the shared project.
  With a key, the rows marked "key-eligible" move to the user's own project.
- The user goes live 30 min after opening the app, streams 2 h on the Stream
  tab, and the app stays open 90 min after the stream.
- The two channel tabs are other channels that stay offline all day. Each is on
  screen 1 h and hidden 3 h, and is selected 3 times. On-screen checks are
  counted at the 2-min rate for the whole hour (the slower 5-min rate is not
  assumed). The deep scan is counted at the 21-unit maximum.
- 150 different people chat during the stream, arriving so that names are
  looked up in about 100 calls (many calls cover only one or two new people).
- The user sends 50 messages to YouTube during the stream.
- The chat stream reconnects once every 10 minutes (13 connections, counting
  the first). This is an assumption, not a measured figure.
- No bans or timeouts, no scheduled broadcast.

| Item | Method | Calculation | Units |
| --- | --- | --- | --- |
| Stream tab, before and after the stream | `liveBroadcasts.list` | 16 checks before going live + 45 after (90 min / 2 min), 1 each | 61 |
| Own channel name | `channels.list` mine | once | 1 |
| Viewer count / uptime refresh | `videos.list` | 120 min / 2 min | 60 |
| Reading the chat | `streamList` | 13 connections x 5 | 65 |
| Chatter names (key-eligible) | `channels.list` | about 100 calls | 100 |
| Sending | `liveChatMessages.insert` | 50 x 20 | 1,000 |
| Two channel tabs (key-eligible) | `playlistItems.list`, `videos.list`, `channels.list` | per tab: scan 21 + 30 on-screen checks + 12 hidden checks + 3 at-once checks = 66; two tabs | 132 |
| **Total** | | | **about 1,420** |

What drives it: sending (1,000 of 1,420) is the user's own typing and is the
most expensive single call after bans. Everything else adds up to about 420
units. If the user has their own API key set, about 230 of those units (names
and the channel tabs) move to the key's project and about 1,190 stay on ours.

Other reference points:

- A user who only watches (no stream of their own, 4 h, two channel tabs, one
  chat read for 2 h, no sending): about 132 + 60 + 65 + 100 = 360 units.
- A signed-in user with the app open and idle all day, one on-screen channel tab
  and no Stream tab: about 300 units; worst case 597.
- Each ban or timeout costs 200 units, each message sent costs 20.

### Why a larger quota is needed

At the default 10,000 units/day, the typical streaming day above (about 1,420
units) fits about 7 users per day, and a user with a Stream tab and an on-screen
channel tab left idle (about 1,020 to 1,317 units) fits about 7 to 9 users. Only
a user with a single idle on-screen channel tab and no Stream tab (about 300
units) fits about 33. These counts are across everyone who signs in with the
built-in client. Expected users:
`[PLACEHOLDER: number of daily active users]`. Quota requested:
`[PLACEHOLDER: users x about 1,420 for streamers, about 300 for viewers with no Stream tab, plus headroom]`
units/day.

## What was done to keep usage low (the changes in this release)

- Idle polling was slowed from every 60 s to the schedule above, with the check
  rate depending on the tab being on screen, a known scheduled start, and how
  long the channel has been offline.
- A channel's uploads playlist is remembered after the first scan; re-checks read
  one page, and one `videos.list` only when something on it changed or is
  upcoming or live.
- A user's own API key (optional) is used first for public reads, so users who
  set one spend their own quota, not the shared one.
- The app keeps track of used-up quota per credential, using Google's reset time
  (midnight Pacific), shows when reading resumes, makes no calls while a pool is
  used up, and starts again by itself after the reset with a random 1 to 10 min
  delay so users do not all return at once.
- Quota bookkeeping uses the true costs (a ban is 200 units).

## Known risk to disclose

- If streaming (gRPC) reads fail three times in a row for a signed-in user, the
  app falls back to polling `liveChatMessages.list`: 5 units per poll, at most
  one per 12 s, so up to 1,500 units per hour for that user while it lasts (more
  when it drains extra pages). It is a fallback, and the app stays on REST for
  that chat until the chat ends or a REST call fails (then it tries gRPC again).
  We have no measured rate of how often this happens. This is the largest
  uncontrolled cost in the app.
- The stats refresh, the name lookups and the stream reconnects during a live
  stream were not changed in this release (their cost is the assumed part of the
  typical day).

## Data handling and policy compliance

All of this matches https://chattersyn.redcyberdragon.net/privacy.html (last
updated 1 November 2026); re-read it before submitting.

- Use of Google data is limited to the features listed above. Not used for
  advertising, not sold, not shared. The app's use and transfer of information
  received from Google APIs adheres to the Google API Services User Data Policy,
  including the Limited Use requirements.
- No ChatterSyn server stores YouTube data. Chat messages go from YouTube to the
  user's computer only.
- Sign-in tokens are kept in the operating system's credential store (macOS
  Keychain, Windows Credential Manager, Secret Service on Linux). The user can
  disconnect in Settings > Accounts, which deletes them, and can revoke access in
  their Google Account.
- The sign-in relay only passes the one-time code and the refresh request to
  Google, adds the app's credential, and does not store or log tokens.
- YouTube display names are held in memory until the app closes.
- The user can have chat saved to local text files (on by default, switch in
  Settings > General). Logs of YouTube chats are deleted automatically after 30
  days (the YouTube API Services limit on keeping API data), and images
  (avatars, badges) in the local cache are removed after 30 days.
- The privacy page links the YouTube Terms of Service and the Google Privacy
  Policy, and sign-in uses Google's own consent screen.

## Items to double-check before submitting

- The unit costs are as of 2026-11-08; re-check Google's table.
- Fill every placeholder; do not paste secrets.
- Measure a real 2-hour stream (number of streamList connections, name lookups,
  messages sent) and replace the assumptions in the typical-day table.
- Confirm the OAuth consent screen, privacy policy URL and the demo video match
  what the form asks for, and that the app is published to production.
