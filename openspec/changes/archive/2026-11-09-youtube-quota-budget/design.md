# Design: youtube-quota-budget

## Context (facts checked 2026-11-08)

- Google quota table (developers.google.com/youtube/v3/determine_quota_cost):
  channels/playlistItems/videos/liveBroadcasts.list = 1 unit; search.list = 1
  unit in its own 100-calls/day bucket; liveChatMessages.list and streamList =
  5; liveChatMessages.insert = 20; liveChatBans.insert/delete = **200** (the
  code's QUOTA["ban"] says 50). Default 10,000 units/day **per project**;
  resets at **midnight Pacific Time** (QuotaTracker rolls over at local midnight).
- Quota is free, never billed, and cannot be bought: when the pool runs out,
  requests fail with quotaExceeded until midnight PT. More quota is granted
  free only after a Google compliance audit (quota-extension form). The cost of
  overspending is an outage, not money.
- With the built-in Google client every signed-in user spends ChatterSyn's one
  project quota. `YouTubeApi._auth` prefers the OAuth token over a user's own
  API key, so even users who set a key spend the shared pool.
- Channel targets (`@handle`, `UC…`) re-resolve: channels.list (1) + uploads
  playlist pages (1 each) + videos.list per page (1 each); `_newest_videos`
  stops paging only when a live video is found, up to 10 pages.
- Intervals: offline/ended 60 s (RE_RESOLVE_INTERVAL), upcoming 30 s
  (UPCOMING_RE_RESOLVE), own Stream tab offline 120 s (MINE_RE_RESOLVE_INTERVAL).
- Measured with the real resolver + intervals (fake API, scratch probe):

  | watch | state | units/day |
  | --- | --- | --- |
  | channel tab, 120 uploads | offline | 10,080 |
  | channel tab, 120 uploads | stream scheduled | 20,160 |
  | channel tab, 1,000 uploads | stream scheduled | 60,480 |
  | video-URL tab | scheduled | 2,880 |
  | Stream tab (mine:) | no broadcast | 720 |
  | Stream tab (mine:) | broadcast "ready" | 2,880 |

- While live: stats videos.list every 120 s (720/day), streamList 5 units per
  (re)connect, YouTubeNames channels.list 1 unit per 0.3 s batch of new
  chatters, sends 20 each.
- Quota exhaustion during resolve ends the watch for good
  (`_watch_iteration` returns False) and `watch()` on an existing key is a
  no-op, so a tab only recovers on restart or reopen.
- Tabs persist and are restored at startup (`general.restore_tabs`, default on).

## Goals / Non-Goals

Goals:

- An idle YouTube tab spends hundreds of quota units a day, not thousands (D3, D4, D7).
- Users who set their own API key spend their own quota for public reads (D2, D11).
- A tab whose quota ran out recovers on its own after the reset (D5).
- The quota estimate follows Google's reset time and costs (D1, A1, A2).

Non-Goals:

- Live-time spend: name lookups, the stats refresh, stream reconnects (D1).
- Sharing one watch between tabs on the same target (D8).
- A per-install daily cap (D10).
- Submitting the quota-extension request: the user does that; this change only
  drafts the answers (D9).

## Open questions

None.

## Decision log

<!-- grillspec: depth=standard council=no budget=15 asked=15 -->

### Open

### Settled

- [x] D1 Scope — **offline/upcoming polling cost + recovery after quota exhaustion + honest quota accounting (bans 200 units, reset at midnight PT)**; live-time spend (name lookups, stats refresh, stream reconnects; former Q6) is OUT of scope, a later change once measured · accepted · alternatives: polling + recovery only (tracker stays wrong); full budget incl. live spend (larger, unmeasured) · reversible: cheap
  > asked first "we arent talking about money right?" — confirmed: quota is free; overspending means an outage for all users until midnight PT
- [x] D2 Quota owner — **the user's own API key first for public reads** (channel polling, video lookups, stats, gRPC reading of public chats) when one is set; OAuth only for what needs it (sends, bans, the own Stream tab `mine:`, the REST reader); fall back to OAuth when the key is rejected · accepted · alternatives: keep OAuth first (all reads on the shared pool); require an own key for channel tabs (breaks one-click sign-in) · reversible: cheap
- [x] D3 Channel live detection — **remember the channel's uploads playlist after the first lookup; re-checks read only the newest uploads page + their live status (≤ 2 units, 1 when nothing changed); a full scan (up to 10 pages) runs once when the watch starts**, so a long-running 24/7 stream is still found · accepted · alternatives: newest page only (misses old 24/7 streams); public RSS feed (0–1 unit, but not the official API, may draw audit questions) · reversible: cheap
- [x] D4 Re-check intervals — **adaptive + schedule-aware**: offline every 2 min, slowing to every 5 min after 30 min offline; scheduled (known start time) every 15 min until 5 min before the start, then every 30 s; "upcoming" without a start time (e.g. a `ready`/`created` broadcast) counts as offline; the own Stream tab (`mine:`) keeps its 2-min offline check · accepted · alternatives: fixed 5 min (simplest, always up to 5 min late); fixed 2 min (~1,440 units/day per idle tab) · reversible: cheap
  > budget math: ~300–600 units/day per idle tab open 24 h; a stream is noticed within 5 min
- [x] D5 Recovery after quota exhaustion — **the watch stays alive in a "quota used up" state showing "resumes at HH:MM" (local time) and restarts on its own shortly after midnight PT, with a few minutes of random delay** so clients don't all hit YouTube at once; sending stays blocked until then · accepted · alternatives: retry every 30 min (keeps knocking on an empty pool); manual Retry button (unnoticed = no chat) · reversible: cheap
- [x] D7 Hidden tabs — **tabs not on screen (another tab selected, or the window minimized) re-check every 15 min instead of the D4 rate; switching to a tab or restoring the window checks it right away** (1–2 units) · accepted · alternatives: same speed for all tabs; pause hidden tabs entirely (background go-live noticed only on open) · reversible: cheap
- [x] D8 Duplicate targets — **no sharing: two tabs on the same YouTube target keep separate watches** (rare; sharing needs new subscribe plumbing) · accepted · alternatives: one shared watch per target · reversible: cheap (can be added later)
- [x] D9 Quota extension — **in scope as a non-code task: the agent drafts the answers for Google's YouTube quota-extension request (calls made, frequency, per-user cost after these fixes); the user submits it in Google Cloud Console** · accepted · alternatives: separate, later · reversible: cheap
- [x] D10 Per-install cap — **none for now**; per-tab intervals bound the cost and the quota extension (D9) is the fix at scale · accepted · alternatives: cap shared-pool polling at 1,500 units/day per install, then 30-min checks · reversible: cheap (add later from usage data)
- [x] D11 Own-key failure — **an invalid key (deleted, API not enabled, rejected) falls back to the shared pool so chat keeps working, with a note in Settings; a key that used up its own quota does NOT spill — that user's public reads wait for midnight PT (D5)** · accepted · alternatives: always spill (heavy user drains both pools); never spill (a typo'd key stops reads) · reversible: cheap
- [x] D12 Stream tab and hidden tabs — **the own Stream tab (`mine:`) is exempt from D7: it keeps its 2-min check while hidden** (≤ 720 units/day, only for streamers who keep it open) · accepted · alternatives: apply D7 (chat may attach up to 15 min late) · reversible: cheap
- [x] D13 Late or never-started scheduled streams — **30-s checks continue until 15 min after the scheduled start, then the offline rules of D4 apply** (2 min, slowing to 5 min) · accepted · alternatives: 1 h grace (~240 units per never-started stream); no grace (up to 2 min late for a slightly late start) · reversible: cheap
- [x] A1 Midnight PT is computed with Qt's `QTimeZone("America/Los_Angeles")` — no new dependency (Python `zoneinfo` would need the `tzdata` package on Windows) · agent's call · reversible: cheap
- [x] A2 The quota tracker counts liveChatBans insert/delete at 200 units (Google's table) · agent's call · reversible: cheap
- [~] A3 "On screen" (D7) = the selected tab, or either pane of a side-by-side split, while the window is not minimized · agent's call · reversible: cheap · superseded by A9
- [x] A4 The resume delay after midnight PT (D5) is random between 1 and 10 minutes · agent's call · reversible: cheap
- [x] A5 A hidden tab keeps the 15-min rate (D7) even inside the 30-s window around a scheduled start (D4/D13); showing the tab checks it at once · agent's call · reversible: cheap
- [~] A6 "30 min offline" (D4) counts from the moment the watch last saw the stream live, or from when the watch started · agent's call · reversible: cheap · superseded by A12
- [~] A7 "1 unit when nothing changed" (D3): videos.list is skipped when the newest uploads page lists the same videos as the last check and none of them was scheduled · agent's call · reversible: cheap · superseded by A11
- [x] A8 Live chat reading and the 2-min stats refresh keep running in hidden tabs (live-time spend is out of scope, D1) · agent's call · reversible: cheap
- [x] A9 "On screen" (D7) = the selected tab while the window is not minimized; the notebook shows one tab at a time ("side by side" is a Twitch | YouTube split inside one tab, checked in ui/tab.py) · agent's call · reversible: cheap
- [x] A10 Quota exhaustion belongs to a pool and is app-wide: the shared pool (calls made with the sign-in) and the user's key pool are tracked separately. When YouTube reports the quota used up on any call of a pool (check, chat reading, send, ban), every tab's calls on that pool wait (D5) at once; calls on the other pool continue (key out → sends, bans, the Stream tab keep working; shared pool out → key reads keep working) · agent's call (follows from D2, D5, D11) · reversible: cheap
- [x] A11 (supersedes A7) videos.list is skipped only when the newest uploads page lists the same videos as the previous check AND none of them is upcoming (with or without a start time) or live — so a `ready` video going live is still seen · agent's call (builder-check: A7 would miss it) · reversible: cheap
- [x] A12 (supersedes A6) "30 min offline" (D4) counts from the latest of: the watch last saw the stream live, the watch started, or a missed scheduled start's 15-min grace ended (D13 says the fallback starts at 2 min) · agent's call · reversible: cheap
- [x] A13 Name lookups for new chatters and user-card channel lookups are public reads under D2 (the key when set) · agent's call · reversible: cheap
- [x] A14 An "invalid key" (D11) is one Google rejects as invalid, expired, restricted, or for an API that is not enabled; "used up" is a quota-exceeded error; a rate-limit error is neither (normal retry). The plan verifies Google's exact error reasons for REST and gRPC. After an invalid-key error the key counts as not working until the user changes it in Settings or restarts the app; the Settings note clears then. Not signed in + invalid key: reads stop and the tab shows auth required, as with no key · agent's call · reversible: cheap
- [x] A15 Text and place: the tab's YouTube status line shows "YouTube quota used up · resumes at HH:MM" (local time); the YouTube send toggle is off and disabled with the tooltip "YouTube quota used up — resumes at HH:MM", so a dual send goes to Twitch only (existing toggle behaviour); a YouTube-only send while waiting is refused with that same text. Settings → Accounts shows under the API key field "API key not working — using your Google sign-in", or "API key not working — YouTube reading paused" when not signed in · agent's call · reversible: cheap
- [x] A16 A tab coming on screen re-checks at once only when its last check is more than 60 s old, so rapid tab switching costs at most one check per tab per minute · agent's call · reversible: cheap
- [x] A17 One random resume delay (1–10 min, D5/A4) is drawn per pool per reset, so every tab shows the same resume time · agent's call · reversible: cheap
- [~] A18 With several scheduled streams among the newest uploads, the earliest scheduled start time sets the rhythm (D4, D13) · agent's call · reversible: cheap · superseded by A21
- [x] A19 The quota-extension draft (D9) is a document in the change folder, `quota-extension.md`, listing every YouTube API method the app calls, how often, and the per-user daily cost after this change; it is a plan task, not a spec requirement (specs state system behaviour) · agent's call · reversible: cheap
- [x] A20 The quota estimate (D1) is kept per pool; a timeout (temporary liveChatBans.insert) counts 200 units like a ban · agent's call · reversible: cheap
- [x] D14 Stream tab with a scheduled own broadcast — **never slower than every 2 min: 2-min checks as usual, plus every 30 s from 5 min before to 15 min after the scheduled start** · accepted · alternatives: the general 15-min scheduled rule (early go-live attaches up to 15 min late); always 2 min with no 30-s window · reversible: cheap
- [x] D15 Chats the user's key cannot read (e.g. members-only) — **when YouTube refuses the key for a chat and the user is signed in, that chat is read with the sign-in** (members-only keeps working as today); other reads keep using the key · accepted · alternatives: no fallback ("chat unavailable", a regression for key users) · reversible: cheap
- [x] D16 Channel tab whose handle or id is not found — **re-check every 15 min** (~96 units/day), on screen or not; still recovers by itself from a passing glitch · accepted · alternatives: offline rhythm (~300–600/day for a likely typo); stop checking (a glitch leaves the tab dead) · reversible: cheap
- [x] A21 (supersedes A18) With several scheduled streams, the earliest start time whose 15-min grace (D13) has not ended sets the rhythm; stale upcoming streams long past their start are ignored for the rhythm · agent's call (builder re-check #1) · reversible: cheap
- [x] A22 Per D2 ("the own Stream tab → OAuth"), everything the Stream tab does uses the sign-in: checks, chat reading and viewer statistics (the own stream may be unlisted or private) · agent's call · reversible: cheap
- [x] A23 Only errors that name the key itself (invalid, expired, restricted, API not enabled) mark the key as not working (D11/A14); any other refusal of the key for one chat is "refused for this chat" (D15). Not signed in + refused for a chat: that chat shows "chat unavailable" as today. The plan verifies the exact REST/gRPC error reasons · agent's call · reversible: cheap
- [x] A24 At resume (D5) the YouTube send toggle is enabled again and returns to its on/off state from before the block (existing `set_youtube_send_enabled` behaviour) · agent's call · reversible: cheap
- [x] A25 A tab whose reads continue (key pool fine) but whose sign-in calls wait (shared pool out) keeps its normal YouTube status; only its send toggle is off and disabled with the "resumes at" tooltip · agent's call · reversible: cheap
- [x] A26 Rhythm boundaries are honoured: the next check is the earlier of (last check + current interval) and the next rhythm boundary (5 min before a scheduled start; the 30-min offline mark is a boundary only for slowing down, so it never makes a check earlier) · agent's call · reversible: cheap
- [x] A27 On any visibility change the next check is rescheduled to last check + the interval of the new state, except that coming on screen checks at once when the last check is over 60 s old (A16) · agent's call · reversible: cheap
- [x] A28 A channel that was not found and is found later starts its watch with the deep scan (D3); the not-found status text stays "not found — check the URL/handle" (existing) · agent's call · reversible: cheap
- [x] A29 The used-up state is kept in memory only (an app restart learns it again from the first failing call); a tab opened while a pool is used up waits without making a call · agent's call · reversible: cheap
