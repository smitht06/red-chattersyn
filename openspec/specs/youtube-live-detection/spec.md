# youtube-live-detection Specification

## Purpose

Defines how a ChatterSyn tab notices that a YouTube stream started and how often it
checks while the stream is not live, so that tabs find streams promptly while
spending as little of the shared daily YouTube quota as possible.

Terms: a **channel tab** watches a YouTube channel given as `@handle` or channel id;
a **video tab** watches one video given as a URL or video id; the **Stream tab**
watches the signed-in user's own broadcast. A tab is **on screen** when it is the
selected tab and the window is not minimized.

## Requirements

### Requirement: Deep scan only when a channel watch starts
When a channel tab starts watching (tab opened, app started with the tab restored,
or the watch resumed), ChatterSyn SHALL look the channel up and scan up to 10
pages of its uploads for a live stream, stopping early when one is found.

#### Scenario: YQB-1 long-running stream found at start
- **WHEN** a channel tab starts watching a channel whose live stream is older than
  its 50 newest uploads
- **THEN** the live stream is found and its chat is read

#### Scenario: YQB-2 live stream on the first page
- **WHEN** a channel tab starts watching a channel whose live stream is among its
  newest uploads
- **THEN** the scan stops after the first page and the chat is read

### Requirement: Cheap re-checks of a channel tab
After the first check, every re-check of a channel tab SHALL read only the newest
page of the channel's uploads and their live status, without looking the channel up
again. A re-check SHALL cost at most 2 quota units. It SHALL cost 1 unit when the
newest uploads are the same as at the previous check and none of them is upcoming
(with or without a start time) or live.

#### Scenario: YQB-3 re-check of an offline channel with many uploads
- **WHEN** a channel tab re-checks an offline channel that has 1,000 uploads
- **THEN** the re-check costs at most 2 quota units

#### Scenario: YQB-4 nothing changed since the last check
- **WHEN** a channel tab re-checks, the newest uploads are the same as at the
  previous check, and none of them is upcoming or live
- **THEN** the re-check costs 1 quota unit

#### Scenario: YQB-5 stream starts on an offline channel
- **WHEN** a channel that a channel tab is re-checking starts a live stream
- **THEN** the next re-check finds the stream and its chat is read

#### Scenario: YQB-35 upcoming video without a start time goes live
- **WHEN** the newest uploads are unchanged but one of them is an upcoming video with
  no start time, and that video goes live
- **THEN** the next re-check finds the stream and its chat is read

### Requirement: Offline re-check rhythm
An on-screen channel tab or video tab whose stream is offline SHALL re-check every
2 minutes, and every 5 minutes once it has been offline for 30 minutes. The 30
minutes count from the latest of: the watch last saw the stream live, the watch
started, or a missed scheduled start's 15-minute grace ended. A stream that is
upcoming without a scheduled start time SHALL count as offline.

#### Scenario: YQB-6 recently offline
- **WHEN** an on-screen channel tab's stream has been offline for 10 minutes
- **THEN** the tab re-checks every 2 minutes

#### Scenario: YQB-7 offline for a long time
- **WHEN** an on-screen channel tab's stream has been offline for more than 30
  minutes
- **THEN** the tab re-checks every 5 minutes

#### Scenario: YQB-8 upcoming without a start time
- **WHEN** an on-screen tab's newest stream is upcoming but has no scheduled start
  time
- **THEN** the tab re-checks as if the stream were offline

### Requirement: Scheduled stream rhythm
An on-screen channel tab or video tab whose stream is scheduled with a known start
time SHALL re-check every 15 minutes until 5 minutes before the start time, then
every 30 seconds until 15 minutes after it; from then on, while the stream is not
live, the offline rhythm SHALL apply, starting at 2 minutes.

#### Scenario: YQB-10 stream scheduled days ahead
- **WHEN** an on-screen tab's stream is scheduled to start in 2 days
- **THEN** the tab re-checks every 15 minutes

#### Scenario: YQB-11 close to the start time
- **WHEN** an on-screen tab's scheduled stream starts in 4 minutes
- **THEN** the tab re-checks every 30 seconds

#### Scenario: YQB-12 stream starts late
- **WHEN** an on-screen tab's stream is 10 minutes past its scheduled start time and
  not live yet
- **THEN** the tab still re-checks every 30 seconds

#### Scenario: YQB-13 stream never starts
- **WHEN** an on-screen tab's stream is more than 15 minutes past its scheduled
  start time and not live
- **THEN** the tab re-checks every 2 minutes, and every 5 minutes once 30 more
  minutes have passed

### Requirement: Which scheduled start sets the rhythm
With several scheduled streams, the earliest start time whose 15-minute grace has
not ended SHALL set the rhythm; upcoming streams further past their start time SHALL
be ignored for it. A check SHALL be made at the 5-minute mark before that start even
if the current interval has not elapsed yet.

#### Scenario: YQB-36 several scheduled streams
- **WHEN** a channel has one stream scheduled in 3 hours and another in 2 days
- **THEN** the tab's rhythm follows the stream scheduled in 3 hours

#### Scenario: YQB-52 stale scheduled stream
- **WHEN** a channel has one upcoming stream whose start time passed 2 days ago and
  another scheduled in 3 hours
- **THEN** the tab's rhythm follows the stream scheduled in 3 hours

#### Scenario: YQB-53 the 5-minute mark is not missed
- **WHEN** an on-screen tab last checked 12 minutes before a scheduled start
- **THEN** its next check is made 5 minutes before the start, not 3 minutes before

### Requirement: Stream tab rhythm
While the user's own broadcast is not live, the Stream tab SHALL re-check every 2
minutes, however long it has been offline and whether or not it is on screen. When
the own broadcast is scheduled with a start time, the Stream tab SHALL also re-check
every 30 seconds from 5 minutes before to 15 minutes after that start time.

#### Scenario: YQB-9 Stream tab while offline
- **WHEN** the user's own broadcast has been offline for more than 30 minutes, or
  exists only as a broadcast with no scheduled start time
- **THEN** the Stream tab re-checks every 2 minutes

#### Scenario: YQB-19 Stream tab not on screen
- **WHEN** the Stream tab is not on screen and the user's own broadcast is offline
- **THEN** the Stream tab still re-checks every 2 minutes

#### Scenario: YQB-37 own broadcast scheduled
- **WHEN** the user's own broadcast is scheduled to start in 2 hours
- **THEN** the Stream tab re-checks every 2 minutes

#### Scenario: YQB-38 own broadcast close to its start time
- **WHEN** the user's own scheduled broadcast starts in 4 minutes
- **THEN** the Stream tab re-checks every 30 seconds

### Requirement: Tabs that are not on screen
A channel tab or video tab that is not on screen SHALL re-check every 15 minutes
while its stream is not live, also during the 30-second window around a scheduled
start. On any visibility change the next check SHALL move to the last check plus
the new interval, except that coming on screen (selected, or the window restored)
SHALL check at once when the last check is over 60 seconds old. Chat reading and
viewer statistics of a live stream SHALL continue unchanged off screen.

#### Scenario: YQB-14 another tab is selected
- **WHEN** the user selects another tab while a channel tab's stream is offline
- **THEN** the channel tab re-checks every 15 minutes

#### Scenario: YQB-15 window minimized
- **WHEN** the user minimizes the window
- **THEN** every channel tab and video tab whose stream is not live re-checks every
  15 minutes

#### Scenario: YQB-16 hidden tab near a scheduled start
- **WHEN** a tab that is not on screen has a stream scheduled to start in 4 minutes
- **THEN** the tab still re-checks every 15 minutes

#### Scenario: YQB-17 selecting a hidden tab
- **WHEN** the user selects a tab that was not on screen and whose last check is more
  than 60 seconds old
- **THEN** the tab re-checks at once and then follows the on-screen rhythm

#### Scenario: YQB-18 restoring the window
- **WHEN** the user restores the minimized window and the selected tab's last check
  is more than 60 seconds old
- **THEN** the selected tab re-checks at once

#### Scenario: YQB-54 a tab leaves the screen
- **WHEN** an offline channel tab that last checked 1 minute ago stops being on screen
- **THEN** its next check is made 15 minutes after that last check, not 1 minute
  from now

#### Scenario: YQB-39 switching back and forth quickly
- **WHEN** the user selects a tab whose last check was 20 seconds ago
- **THEN** no extra check is made and the tab keeps its on-screen rhythm

#### Scenario: YQB-20 live chat in a hidden tab
- **WHEN** a tab that is not on screen is reading a live stream's chat
- **THEN** new chat messages keep arriving in that tab and its viewer count keeps
  updating

### Requirement: Channel not found
A channel tab whose `@handle` or channel id is not found SHALL show "not found —
check the URL/handle" and re-check every 15 minutes, on screen or not. When the
channel is found later, its watch SHALL start as described under the deep scan.

#### Scenario: YQB-40 handle not found
- **WHEN** a channel tab's handle is not found
- **THEN** the tab shows "not found — check the URL/handle" and re-checks every 15
  minutes

#### Scenario: YQB-41 channel found later
- **WHEN** a channel that was not found at the previous check is found and live
- **THEN** the tab scans its uploads as at watch start and reads the live chat
