## Purpose

Defines whose YouTube quota pays for which calls, how ChatterSyn estimates its
quota use, and how tabs behave when the quota runs out, so one user cannot drain
the daily quota that every user of the built-in Google sign-in shares.

Terms: the **shared pool** is the daily quota of ChatterSyn's built-in Google
project, spent by calls made with the Google sign-in; the **key pool** is the daily
quota of the user's own API key, when one is set.

## ADDED Requirements

### Requirement: The user's own API key pays for public reads
When the user has set their own YouTube API key, ChatterSyn SHALL make public reads
with it: channel and video checks, viewer statistics, name lookups, user-card
lookups, and reading live chats through the streaming reader. Sending, bans and
unbans, everything the Stream tab does (checks, chat reading, viewer statistics)
and the REST chat reader SHALL use the Google sign-in. Without an API key, every
call SHALL use the sign-in.

#### Scenario: YQB-21 channel checks with an API key
- **WHEN** a signed-in user who set an API key has a channel tab open
- **THEN** the channel tab's checks are made with the API key, not the sign-in

#### Scenario: YQB-22 reading a public chat with an API key
- **WHEN** a signed-in user who set an API key opens a public live stream
- **THEN** its chat is read with the API key

#### Scenario: YQB-23 sending still uses the sign-in
- **WHEN** a signed-in user who set an API key sends a message to YouTube chat
- **THEN** the message is sent with the sign-in

#### Scenario: YQB-24 Stream tab still uses the sign-in
- **WHEN** a signed-in user who set an API key has the Stream tab open while their
  own broadcast is live
- **THEN** the checks, the chat reading and the viewer statistics of the Stream tab
  use the sign-in

#### Scenario: YQB-25 no API key
- **WHEN** a signed-in user has not set an API key
- **THEN** all YouTube calls use the sign-in

### Requirement: Chats the key cannot read
When YouTube refuses the user's API key for one live chat (for example a
members-only stream) without naming the key itself as invalid, and the user is
signed in, ChatterSyn SHALL read that chat with the sign-in. Other reads SHALL keep
using the key, and the key SHALL NOT be marked as not working. Without a sign-in,
that chat SHALL show "chat unavailable".

#### Scenario: YQB-42 members-only chat
- **WHEN** a signed-in user who set an API key opens a members-only stream they can
  watch
- **THEN** its chat is read with the sign-in, messages arrive as without a key, and
  Settings shows no "API key not working" note

#### Scenario: YQB-55 members-only chat without a sign-in
- **WHEN** a user who set an API key but is not signed in opens a members-only stream
- **THEN** that tab shows "chat unavailable" and other tabs keep reading with the key

### Requirement: A failing API key
When Google rejects the API key as invalid, expired, restricted, or for an API that
is not enabled, ChatterSyn SHALL make those reads with the sign-in when signed in,
until the user changes the key in Settings or restarts the app. Settings → Accounts
SHALL show under the key field "API key not working — using your Google sign-in", or
"API key not working — YouTube reading paused" when not signed in.

#### Scenario: YQB-26 invalid key with a sign-in
- **WHEN** Google rejects a signed-in user's API key as invalid during a channel check
- **THEN** the check is repeated with the sign-in, the tab keeps working, and
  Settings → Accounts shows "API key not working — using your Google sign-in"

#### Scenario: YQB-43 invalid key without a sign-in
- **WHEN** Google rejects the API key of a user who is not signed in
- **THEN** YouTube reading stops, the tab shows that authorization is required, and
  Settings → Accounts shows "API key not working — YouTube reading paused"

#### Scenario: YQB-44 key changed in Settings
- **WHEN** the user enters a different API key in Settings after it was rejected
- **THEN** public reads use the new key and the "API key not working" note is gone

#### Scenario: YQB-45 rate limit is not an invalid key
- **WHEN** a read with the API key hits YouTube's short-term rate limit
- **THEN** the read is retried as usual and the key stays in use

### Requirement: Quota pools run out separately
Running out of quota SHALL apply to one pool across the whole app. When YouTube
reports the quota used up for any call of a pool (a check, chat reading, a send, a
ban), every tab's calls on that pool SHALL wait for the reset at once, and a tab
opened meanwhile SHALL wait without a call. Calls on the other pool SHALL continue.
A key pool that ran out SHALL NOT move its reads to the shared pool. The used-up
state is not kept across an app restart.

#### Scenario: YQB-27 key out of quota
- **WHEN** the user's API key has used up its daily quota
- **THEN** no read moves to the sign-in, the tabs whose reads use the key show
  "YouTube quota used up · resumes at HH:MM", and sending to YouTube keeps working

#### Scenario: YQB-46 shared pool out of quota
- **WHEN** YouTube reports the shared pool used up while one tab sends a message
- **THEN** every tab's sign-in calls wait for the reset, and reads made with the
  user's API key keep working

#### Scenario: YQB-56 tab opened while a pool is used up
- **WHEN** the user opens a new tab whose calls use a pool that is used up
- **THEN** the tab shows "YouTube quota used up · resumes at HH:MM" without making a
  call

#### Scenario: YQB-47 other tabs follow at once
- **WHEN** one tab learns that a pool is used up
- **THEN** every other tab that uses that pool shows "YouTube quota used up ·
  resumes at HH:MM" without making a call of its own

### Requirement: A tab waits when the quota runs out
A tab whose reads wait SHALL keep its YouTube watch and SHALL show "YouTube quota
used up · resumes at HH:MM" (local time) in its YouTube status; while the shared
pool is out, its YouTube send toggle SHALL be off and disabled with that text as
tooltip, and a tab whose reads continue keeps its normal status. This SHALL hold
for all tab kinds, during a check or while reading chat.

#### Scenario: YQB-28 quota runs out during a check
- **WHEN** YouTube reports the shared pool used up while a tab checks for a stream
- **THEN** the tab shows "YouTube quota used up · resumes at HH:MM" and its YouTube
  send toggle is off and disabled

#### Scenario: YQB-30 quota runs out while reading chat
- **WHEN** YouTube reports the quota used up while a video tab is reading a live
  chat
- **THEN** the video tab does not stop watching and resumes after the next midnight
  Pacific Time

#### Scenario: YQB-57 reads continue, sends wait
- **WHEN** the shared pool is used up and a tab reads its chat with the user's API
  key
- **THEN** the tab keeps its normal YouTube status, chat keeps arriving, and its
  YouTube send toggle is off and disabled

#### Scenario: YQB-48 dual send while waiting
- **WHEN** the user sends a message in a tab with Twitch and YouTube while the shared
  pool is used up
- **THEN** the message goes to Twitch only

#### Scenario: YQB-49 YouTube-only send while waiting
- **WHEN** the user tries to send to YouTube only while the shared pool is used up
- **THEN** the send is refused with "YouTube quota used up — resumes at HH:MM"

### Requirement: A tab resumes on its own after the reset
Waiting calls SHALL resume on their own at one random moment, per pool and reset,
between 1 and 10 minutes after the next midnight Pacific Time, so every tab shows the
same resume time. Each waiting watch SHALL start again as if its tab had just been
opened, without a restart or reopening the tab, and the YouTube send toggle SHALL be
enabled again in the on/off state it had before it was blocked.

#### Scenario: YQB-29 resuming after the reset
- **WHEN** the resume time shown by a tab has passed
- **THEN** the tab checks again without a restart or reopening, and reads the chat
  if the stream is live

#### Scenario: YQB-31 resume time is after midnight Pacific
- **WHEN** the quota runs out at 23:00 Pacific Time
- **THEN** the tab resumes between 00:01 and 00:10 Pacific Time, shown in the
  user's local time

#### Scenario: YQB-58 send toggle after the reset
- **WHEN** a tab's YouTube send toggle was on before the shared pool ran out, and the
  resume time passes
- **THEN** the toggle is enabled and on again

#### Scenario: YQB-50 same resume time in every tab
- **WHEN** three tabs wait for the same pool
- **THEN** all three show the same resume time

### Requirement: Quota estimate follows Google's rules
ChatterSyn SHALL keep a separate estimate of today's quota use for each pool. Each
estimate SHALL start a new day at midnight Pacific Time and SHALL count each ban,
timeout and unban at 200 units.

#### Scenario: YQB-32 local midnight is not the reset
- **WHEN** local midnight passes but it is not yet midnight Pacific Time
- **THEN** the estimate of today's use is not reset

#### Scenario: YQB-33 midnight Pacific is the reset
- **WHEN** midnight Pacific Time passes
- **THEN** the estimate of today's use starts again from zero

#### Scenario: YQB-34 cost of a ban, timeout or unban
- **WHEN** the user bans, times out or unbans someone in a YouTube chat
- **THEN** the shared pool's estimate grows by 200 units

#### Scenario: YQB-51 reads with the key
- **WHEN** a public read is made with the user's API key
- **THEN** it counts in the key pool's estimate, not the shared pool's
