## Why

Streamers want their merged Twitch + YouTube chat on stream. Today they can only
window-capture ChatterSyn, which shows the whole app and can't be styled. OBS (and
similar tools) can show any web page as a "Browser Source", so ChatterSyn can serve
its Stream tab's chat as a small local page made for video.

## What Changes

- A new Settings page, "Browser Sources", turns on a local page at
  `http://localhost:17830/chat` (port editable) and shows its URL with a Copy button.
- The page shows the Stream tab's chat live: messages, subs, raids, announcements
  and Super Chats, never the app's own notes. Deleted messages and timed-out users'
  messages disappear at once.
- Appearance: font, size, text and background colours, outline; platform icons,
  badges, emotes, timestamps; fade after N seconds; a message limit. Changes reach
  open pages live.

## Capabilities

### New Capabilities
- `browser-source`: the local chat page for OBS and its settings.

### Modified Capabilities

## Impact

New services/overlay_server.py, services/overlay_feed.py, an HTML asset, a Settings
page, MainWindow wiring, DEFAULTS, the PyInstaller spec's data files, README. No new
dependencies; the page listens on 127.0.0.1 only.
