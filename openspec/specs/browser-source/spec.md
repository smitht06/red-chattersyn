# browser-source Specification

## Purpose

Lets a streamer show the Stream tab's chat on stream: ChatterSyn serves it as a
local web page that OBS can add as a Browser Source, styled from a Settings page.

Terms:

- The **page** is `http://localhost:<port>/chat`, served only on this computer
  (127.0.0.1); the default port is 17830.
- The **source chat** is the Stream tab's chat: everything that tab shows that came
  from Twitch or YouTube — messages (including your own), subs, raids,
  announcements, Super Chats and memberships. The app's own notes (connection
  status, errors, command confirmations) are not part of it.
- A row is **removed** when a moderator deletes it or its author is timed out or
  banned (the app greys it out).

## Requirements

### Requirement: Turning the page on and off
Settings SHALL have a page "Browser Sources" with a checkbox "Show chat as a
browser source for OBS" (off by default), the page's URL with a Copy button, the
port (1024–65535, default 17830), and a status line. While the checkbox is on —
including from launch when it was saved on — ChatterSyn SHALL serve the page on
127.0.0.1 at that port; while it is off, nothing SHALL listen.

#### Scenario: OBS-1 off by default
- **WHEN** ChatterSyn starts with default settings
- **THEN** nothing listens on port 17830

#### Scenario: OBS-2 turned on
- **WHEN** the user ticks the checkbox and closes Settings with OK
- **THEN** `http://localhost:17830/chat` answers with the chat page, and the status
  line reads "Running — add it in OBS as a Browser Source."

#### Scenario: OBS-3 copy the URL
- **WHEN** the user presses Copy
- **THEN** the clipboard holds `http://localhost:17830/chat`

### Requirement: Status line
The status line SHALL read "Off." while the page is off, "Running — add it in OBS as
a Browser Source." while it is served, and "Port <port> is in use — choose
another." when the port can't be used; a port that can't be used SHALL NOT stop the
rest of the app.

#### Scenario: OBS-4 port in use
- **WHEN** the page is turned on and another program already uses the port
- **THEN** the status line reads "Port 17830 is in use — choose another." and chat
  keeps working

#### Scenario: OBS-5 port changed
- **WHEN** the user changes the port to 17900 and presses OK
- **THEN** the page moves to `http://localhost:17900/chat` and 17830 is free again

#### Scenario: OBS-6 only this computer
- **WHEN** the page is on
- **THEN** it listens on 127.0.0.1 only

### Requirement: Live chat on the page
The page SHALL show the source chat as it arrives, including subs, raids,
announcements and Super Chats with their colour bars, and SHALL NOT show the app's
own notes. When the page connects or reconnects, it SHALL first show the source
chat's most recent rows that are still within the fade time, up to the message
limit, oldest first. Special rows SHALL use the app row's text and colour bar.
Several pages MAY be open at once and SHALL each get every row.

#### Scenario: OBS-7 a message appears
- **WHEN** a viewer writes "hello" in your Twitch chat while the page is open
- **THEN** the page shows the viewer's name and "hello"

#### Scenario: OBS-8 your own message
- **WHEN** you send a message from ChatterSyn
- **THEN** it appears on the page too

#### Scenario: OBS-9 events shown, notes hidden
- **WHEN** a sub, a raid and a Super Chat arrive, and ChatterSyn adds a
  "Twitch connected" note to the tab
- **THEN** the page shows the sub, the raid and the Super Chat, and not the note

#### Scenario: OBS-10 reconnect
- **WHEN** an OBS scene with the page is switched off and on again
- **THEN** the page shows the recent rows again, oldest first, without duplicates

#### Scenario: OBS-11 two pages
- **WHEN** two browser sources show the page
- **THEN** both show each new message

#### Scenario: OBS-12 before going live
- **WHEN** you're signed in to Twitch but not live on YouTube
- **THEN** the page shows your Twitch chat, with no placeholder text

### Requirement: Pages survive a restart
When ChatterSyn stops, an open page SHALL keep its rows and reconnect by itself once
ChatterSyn serves it again.

#### Scenario: OBS-26 ChatterSyn restarts
- **WHEN** ChatterSyn is closed and started again while OBS shows the page
- **THEN** the page keeps its rows meanwhile and shows new chat again once
  ChatterSyn is back

### Requirement: Removed rows leave the page
When a row of the source chat is removed, the page SHALL take it away at once. An
un-timeout SHALL NOT bring removed rows back.

#### Scenario: OBS-13 deleted message
- **WHEN** a moderator deletes a viewer's message
- **THEN** the message disappears from the page

#### Scenario: OBS-14 timeout
- **WHEN** a viewer is timed out
- **THEN** all of that viewer's messages disappear from the page

#### Scenario: OBS-22 ban, then unban
- **WHEN** a viewer is banned and later unbanned
- **THEN** their messages disappear at the ban and don't come back at the unban

### Requirement: Appearance
The Browser Sources page SHALL offer: font, text size, text colour, background
colour with transparency, a text outline, show platform icons, show badges, show
emotes, show timestamps (HH:MM), fade messages after N seconds (0–600, 0 = keep;
a row fades out once it is N seconds past its arrival), and show at most N messages
(5–200). Changes SHALL reach open pages when Settings is closed with OK, without
reloading them, and apply to rows already shown.

#### Scenario: OBS-16 live restyle
- **WHEN** the user changes the text size to 28 and presses OK while OBS shows the
  page
- **THEN** the page's text becomes 28 px without reloading

#### Scenario: OBS-17 fade
- **WHEN** fade is 30 s and a message is older than 30 s
- **THEN** it is no longer shown

#### Scenario: OBS-18 keep
- **WHEN** fade is 0
- **THEN** messages stay until the message limit pushes them out

#### Scenario: OBS-19 limit
- **WHEN** the limit is 50 and a 51st message arrives
- **THEN** the oldest message leaves the page

#### Scenario: OBS-20 hidden parts
- **WHEN** emotes are switched off
- **THEN** an emote shows as its name in text

#### Scenario: OBS-23 icons and badges off
- **WHEN** platform icons and badges are switched off
- **THEN** rows show neither

#### Scenario: OBS-24 timestamps
- **WHEN** timestamps are switched on
- **THEN** each row starts with its time as HH:MM

#### Scenario: OBS-25 demo mode
- **WHEN** ChatterSyn runs with --demo and the page is on
- **THEN** the page shows the demo's sample chat

### Requirement: Default look
By default the page SHALL use 20 px white text with an outline on a transparent
background, show platform icons, badges and emotes, hide timestamps, fade messages
after 30 s and show at most 50. Names without their own colour SHALL use the text
colour, in bold.

#### Scenario: OBS-15 defaults
- **WHEN** the page is opened with default settings
- **THEN** text is 20 px white with an outline on a transparent background, with
  platform icons, badges and emotes, and no timestamps

### Requirement: Safe page content
The page SHALL show chat text as text (never as markup), and SHALL load images only
from http(s) emote and badge addresses.

#### Scenario: OBS-21 markup in chat
- **WHEN** a viewer writes `<img src=x onerror=alert(1)>`
- **THEN** the page shows those characters as text and runs nothing
