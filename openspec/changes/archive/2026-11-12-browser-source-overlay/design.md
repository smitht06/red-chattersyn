# Design: browser-source-overlay

## Context (facts checked 2026-11-11)

- Every row a tab shows goes through `Tab.add_message` → `MessageModel` (queued, then
  `flush` emits rowsInserted every ~100 ms). Your own sent messages are added there
  too and never pass `bridge.message`.
- Deletions and timeouts change flags in place on the tab's model
  (`mark_deleted` → DELETED; `update_flags_for_user` / `_for_channel` → DISABLED) and
  emit `dataChanged`; un-timeouts clear DISABLED.
- `Message` carries `author` (name, color, badges with `urls`), `runs`
  (`TextRun` / `ImageRun` with an `Emote` that has CDN `urls`), `flags` (SYSTEM,
  ACTION, SUBSCRIPTION, ANNOUNCEMENT, SUPERCHAT, MEMBERSHIP, CHEER, DELETED,
  DISABLED…) and `extra` (Twitch USERNOTICE rows: `msg_id`; YouTube special rows:
  `youtube_type`). App notes are `Message.system(...)` without those extras.
- The Stream tab is `MainWindow._streamer_tabs` (created by `_new_streamer_tab`); in
  `--demo` the default tab is a Stream tab fed by FakeFeed.
- Local servers already exist: the OAuth loopback servers use
  `asyncio.start_server(…, "127.0.0.1", port)` with hand-written HTTP. No web server
  dependency is installed. Flatpak's `--share=network` allows 127.0.0.1 listeners.
- PyInstaller bundles assets from an explicit `datas` list in chattersyn.spec.
- Settings pages are added with `_add_page` and saved in `accept()` via `set_batch`;
  MainWindow re-applies settings in `_apply_settings()`.

## Goals / Non-Goals

Goals: the Stream tab's chat as an OBS browser source with basic styling (D1–D10).

Non-Goals: a live preview in Settings (D3); layout options (top/bottom, alignment)
(D2); sources for other tabs (D1); access from other computers (D3).

## Open questions

None.

## Decision log

<!-- grillspec: depth=standard council=no budget=15 asked=8 -->

### Open

### Settled

- [x] D1 Source — **the Stream tab** (your own Twitch + YouTube chat, follows your channel) · user · alternatives: a tab picked in Settings; one URL per tab · reversible: cheap
- [x] D2 Appearance v1 — **font, size, colours (text, background/transparency, outline); what to show (platform icons, badges, emotes, timestamps); message lifetime (fade after N s or keep; max on screen)** · user · alternatives: also layout (top/bottom, alignment) · reversible: cheap
- [x] D3 Setup — **Settings shows the URL with a Copy button; reachable only from this computer** · user · alternatives: + live preview in Settings · reversible: cheap
- [x] D4 Moderation — **deleted messages and timed-out users' messages disappear from the overlay right away** · user · alternatives: keep them greyed · reversible: cheap
- [x] D5 Port — **17830 by default, editable on the page**; URL `http://localhost:17830/chat` · accepted · alternatives: 8080 · reversible: cheap
- [x] D6 Rows shown — **chat plus subs, raids, announcements and Super Chats/memberships** (with their colour bar, as in the app); the app's own notes never show · accepted · alternatives: chat only; a toggle · reversible: cheap
- [x] D7 Off by default; switched on with the "Show chat as a browser source" checkbox · accepted (stated in the Q9 framing) · reversible: cheap
- [x] D8 Default look — **20 px, white text, dark outline, transparent background, fade after 30 s, at most 50 messages** · accepted · alternatives: 16 px / never fade / 20 · reversible: cheap
- [x] D9 Before you're live — **shows your Twitch chat already (the Stream tab reads it), YouTube joins when your stream starts; no placeholder text** · accepted · alternatives: a "waiting for chat" line · reversible: cheap
- [x] D10 Fill-ins (no product choice): several OBS scenes/browsers may open the URL at once; on (re)connect the page gets the last `max_messages` rows that haven't faded; appearance changes reach open pages live without a refresh; names without a colour (YouTube) use the text colour, bold, like the app; timestamps HH:MM; works in --demo (its "Your Stream" tab feeds it); if the port is taken the page says so and nothing else breaks · agent's call · reversible: cheap
- [x] D11 Spec fill-ins: status texts "Running — add it in OBS as a Browser Source." and "Port <port> is in use — choose another."; an emote with emotes switched off shows as its name; chat text is never interpreted as markup and images load only from http(s) emote/badge addresses · agent's call · reversible: cheap
- [x] D12 Builder-check fill-ins (follow the app or D1–D11): controls = font list (installed fonts, QFontComboBox), size 10–72 px, port 1024–65535 (spin box, no invalid entry possible), fade 0–600 s, limit 5–200; outline is on/off with a fixed dark colour and width; a row fades out over 0.5 s once it is `fade` seconds past its arrival in ChatterSyn, and new fade/limit values apply to rows already shown; replay is oldest first, newest last (with fade 0: the last N rows); subs/raids/announcements/Super Chats/memberships use the app row's text and colour bar (Super Chat amount included), rows without an author show no name or icon; an un-timeout doesn't bring removed rows back; bans remove like timeouts; status line: off → "Off.", on → "Running — add it in OBS as a Browser Source.", port taken → "Port <port> is in use — choose another."; the page starts at launch when saved on; when ChatterSyn stops, open pages keep their rows and reconnect by themselves; nothing is shown on the page when the port is taken (nothing listens), only the status line says so · agent's call · reversible: cheap
