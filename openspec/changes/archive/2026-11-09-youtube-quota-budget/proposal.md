## Why

Every user signed in through ChatterSyn's built-in Google client spends one shared
YouTube quota of 10,000 units a day. One YouTube channel tab left open while the
channel is offline spends 4,000 to 60,000 units a day, so a single user can switch
YouTube off for everyone until midnight Pacific Time. When the quota runs out, a
tab's YouTube watch stops for good and only a restart brings it back.

## What Changes

- Channel tabs scan a channel's uploads deeply only once, when the watch starts;
  re-checks read only the newest uploads (at most 2 units).
- Offline and scheduled tabs re-check on an adaptive, schedule-aware rhythm
  instead of every 60 s (offline) or 30 s (scheduled).
- Tabs that are not on screen re-check every 15 minutes and are checked at once
  when they come on screen; the own Stream tab is exempt.
- A user's own YouTube API key pays for their public reads; the Google sign-in is
  used only where it is required.
- A tab whose quota ran out waits and resumes on its own after midnight Pacific
  Time instead of stopping.
- The app's quota estimate resets at midnight Pacific Time and counts bans at
  their real cost (200 units).
- Non-code: draft the answers for Google's YouTube quota-extension request (the
  user submits it).

## Capabilities

### New Capabilities
- `youtube-live-detection`: how YouTube tabs notice that a stream started, and how often they check.
- `youtube-quota`: whose quota pays for which calls, quota accounting, and recovery after the quota runs out.

### Modified Capabilities
- none (no living specs exist yet)

## Impact

The YouTube service (watch loop, resolver, API client, gRPC reader auth), the main
window (tab visibility, quota status text, send toggle) and Settings → Accounts
(API key note). No new dependencies, no settings schema change.
