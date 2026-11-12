# browser-source-overlay — plan

## Goal

Serve the Stream tab's chat as a local web page OBS can add as a Browser Source,
styled from a new Settings page "Browser Sources".

## Architecture

`overlay_feed.py` turns a `Message` into a JSON event (pure). `overlay_server.py`
serves `/chat` (the page) and `/events` (Server-Sent Events) on 127.0.0.1 with
hand-written HTTP (like the OAuth loopback servers), keeps a replay buffer and fans
events out to every connected page. `OverlayBridge` listens to the Stream tab's
`MessageModel` (`rowsInserted`, `dataChanged`) and publishes. MainWindow starts,
stops and restyles it from settings. The page is one static HTML file with vanilla
JS; all chat text goes in with `textContent`.

**Spec:** openspec/changes/browser-source-overlay/specs/browser-source/spec.md

## Global Constraints

- Commit or push only when the user asks; never add a `Co-Authored-By` trailer (CLAUDE.md).
- Listen on 127.0.0.1 only; GET only; no new dependencies; tests use `127.0.0.1:0` and a raw asyncio client (pattern: `browser_get` in tests/test_twitch_auth.py).
- Exact texts: checkbox "Show chat as a browser source for OBS"; status "Off.", "Running — add it in OBS as a Browser Source.", "Port <port> is in use — choose another."; URL `http://localhost:<port>/chat`.
- Settings block `browser_source`: enabled False, port 17830 (1024–65535), font_family "", font_size 20 (10–72), text_color "#ffffff", background "#00000000", outline True, show_platform_icons True, show_badges True, show_emotes True, show_timestamps False, fade_seconds 30 (0–600), max_messages 50 (5–200).
- Shown rows: Twitch/YouTube rows of the Stream tab, including subs/raids/announcements/Super Chats/memberships; never app notes (system rows without `extra["msg_id"]` or `extra["youtube_type"]`). Images only from http(s) URLs.
- `uv run ruff check src/ tests/` and `uv run pytest -q` stay green after every task.

## Review Focus

1. A slow or dead page (OBS closed mid-write) must not block other pages or the chat → task 1.2 pins `test_stuck_client_does_not_block_others` (per-client bounded queue; drop the client when full).
2. Rows still pending in `MessageModel` get their flags changed without a signal; a row deleted before its flush must never be published → task 2.2 pins `test_row_deleted_before_flush_never_shown`.
3. Replay must not resend rows the page already has after a reconnect (EventSource `Last-Event-ID`) → task 1.2 pins `test_reconnect_with_last_event_id_has_no_duplicates`.
4. The page must never execute chat content → task 3.1 pins `test_page_inserts_text_with_textcontent_only` (static check of chat.html: no `innerHTML`/`insertAdjacentHTML`/`eval`).

## 1. Feed and server

- [x] 1.1 Message → overlay event — verify: uv run pytest tests/test_overlay_feed.py -q passes

Covers: OBS-9, OBS-21 · Test: scenario-first

Files: create src/chattersyn/services/overlay_feed.py; test tests/test_overlay_feed.py

Interfaces:
- `message_to_event(msg: Message) -> dict | None` → `{"id", "platform": "twitch"|"youtube", "name": str, "color": str|None, "badges": [{"url","title"}], "parts": [{"t":"text","v"}|{"t":"emote","url","name"}], "ts": "HH:MM", "arrived": float (epoch s), "kind": "normal"|"action"|"sub"|"announcement"|"superchat"|"membership"|"cheer"|"raid"|"event", "bar": str|None (colour as in ui/message_delegate.py)}`; None for app notes, Platform.SYSTEM, DELETED or DISABLED rows. Badges/emotes without an http(s) URL are dropped (an emote falls back to a text part with its name).

1. Tests: normal row; /me (ACTION); emote and badge URLs; javascript:/data: URL dropped; Twitch sub with `msg_id`; raid (no author); YouTube Super Chat; app note → None; deleted/disabled → None; markup text kept verbatim as text (OBS-21). Run. Expected: FAIL.
2. Implement. 3. Verify, then full suite. 4. Commit `feat(overlay): message to overlay event`.

- [x] 1.2 Local server with Server-Sent Events — verify: uv run pytest tests/test_overlay_server.py -q passes

Covers: OBS-2, OBS-6, OBS-7, OBS-10, OBS-11, OBS-13 · Test: scenario-first

Files: create src/chattersyn/services/overlay_server.py, src/chattersyn/assets/overlay/chat.html (placeholder page; finished in 3.1); test tests/test_overlay_server.py

Interfaces:
- `OverlayServer(*, host="127.0.0.1", port: int, page: bytes)`: `async start() -> bool` (False when the port is taken), `async stop()`, `running`, `port` (actual bound port), `publish_message(event)`, `publish_remove(ids: list[str])`, `set_config(dict)` (stored and pushed), `set_limits(max_messages, fade_seconds)` for the replay buffer.
- HTTP: `GET /chat` → 200 text/html page; `GET /events` → 200 `text/event-stream`, `Cache-Control: no-store`; first a `config` event, then replay (rows within fade time, oldest first, at most max, skipping ids up to `Last-Event-ID`), then live `message`/`remove`/`config` events with incrementing `id:`; `: ping` every 15 s; anything else → 404; non-GET → 405.
- Per-client `asyncio.Queue(maxsize=500)`; a full queue drops that client.

1. Tests (real server on port 0, raw client): `/chat` serves HTML; `/events` sends config then replay; live message reaches two clients (OBS-11); remove event (OBS-13); reconnect with Last-Event-ID has no duplicates (OBS-10 / Review Focus 3); 404/405; port taken → start() False; binds 127.0.0.1 (OBS-6); stuck client doesn't block others (Review Focus 1); stop() closes clients. Run. Expected: FAIL.
2. Implement. 3. Verify, then full suite. 4. Commit `feat(overlay): local server with live events`.

## 2. Settings and wiring

- [x] 2.1 Settings defaults and the Browser Sources page — verify: uv run pytest tests/test_settings_dialog.py tests/test_settings.py -q passes

Covers: OBS-1, OBS-3, OBS-4 · Test: scenario-first

Files: modify src/chattersyn/config/settings.py, src/chattersyn/ui/settings_dialog.py; test tests/test_settings_dialog.py, tests/test_settings.py

Interfaces:
- DEFAULTS `browser_source` block (Global Constraints).
- `SettingsDialog(..., browser_source_status: str = "Off.")`; page "Browser Sources" (10th; update the page-count test) built by `_build_browser_sources()`: checkbox, URL label + "Copy" button (`QGuiApplication.clipboard().setText(url)`; URL follows the port spin live), status label, port spin, `QFontComboBox` + size spin, "Text colour…"/"Background…" buttons (`QColorDialog`, background with alpha) showing the chosen colour, outline + four "show" checkboxes, fade spin (suffix " s", special value text "Keep"), limit spin. Saved in `accept()` via `set_batch`.

1. Tests: defaults present (OBS-1 setting off); page exists with the controls and defaults; Copy puts the URL on the clipboard (OBS-3); status text passed in is shown (OBS-4 text); accept() saves every field; ranges enforced by the spins. Run. Expected: FAIL.
2. Implement. 3. Verify, then full suite. 4. Commit `feat(settings): Browser Sources page`.

- [x] 2.2 Stream tab → server, started from settings — verify: uv run pytest tests/test_main_window_overlay.py -q passes

Covers: OBS-1, OBS-2, OBS-4, OBS-5, OBS-7, OBS-8, OBS-9, OBS-12, OBS-13, OBS-14, OBS-16, OBS-22, OBS-25 · Test: scenario-first

Files: modify src/chattersyn/services/overlay_feed.py (add `OverlayBridge`), src/chattersyn/ui/main_window.py; test tests/test_main_window_overlay.py

Interfaces:
- `OverlayBridge(model: MessageModel, publish_message, publish_remove)`: on `rowsInserted` publish each new row's event; on `dataChanged` publish `remove` for rows that became DELETED or DISABLED (and remember them, so a later un-timeout never re-adds them); `detach()`.
- MainWindow: `_overlay: OverlayServer | None`, `_overlay_status: str`; `async _apply_overlay()` called at startup (also in demo) and from `_apply_settings()`: off → stop ("Off."); on → (re)start when port changed or not running ("Running — …" / "Port N is in use — choose another."), `set_config` + `set_limits` from settings; bridge bound to `self._streamer_tabs[0]`'s model (re-bound when that tab is created or closed); `_open_settings` passes `browser_source_status`; `shutdown()` stops the server.

1. Tests (demo window, port 0 via settings override, raw client): a row added to the Stream tab reaches the page (OBS-7, OBS-25); your own optimistic row reaches it (OBS-8); app note doesn't, sub does (OBS-9); mark_deleted → remove (OBS-13); timeout → remove for all the user's rows (OBS-14); ban + unban → not re-added (OBS-22); off by default → nothing listens (OBS-1); enabling via `_apply_settings` starts it (OBS-2); port change moves it (OBS-5); taken port → status text (OBS-4); restyle pushes config (OBS-16); row deleted before flush never shown (Review Focus 2). Run. Expected: FAIL.
2. Implement. 3. Verify, then full suite. 4. Commit `feat(overlay): Stream tab chat on the browser source`.

## 3. Page, packaging, docs

- [x] 3.1 The overlay page — verify: uv run pytest tests/test_overlay_page.py -q passes

Covers: OBS-10, OBS-15, OBS-16, OBS-17, OBS-18, OBS-19, OBS-20, OBS-21, OBS-23, OBS-24, OBS-26 · Test: scenario-first

Files: modify src/chattersyn/assets/overlay/chat.html; modify chattersyn.spec (datas); test tests/test_overlay_page.py

Interfaces:
- One HTML file: CSS variables from `config` (font, size px, colours, outline via text-shadow, transparent background default); EventSource on `/events` (built-in reconnect keeps rows, OBS-26); rows built with `document.createElement` + `textContent`; platform icons as inline SVG (copied from ui/platform_logos.py `_SVG`); badges/emotes as `<img>` from the event's URLs; timestamps HH:MM when on; each row removed with a 0.5 s fade once `now - arrived > fade_seconds` (0 = keep), re-checked every second and when config changes; trim to `max_messages`; `remove` events delete by id.

1. Tests (static and served): page served by the server contains the EventSource code and the inline SVGs; no `innerHTML`/`insertAdjacentHTML`/`eval`/`document.write` (Review Focus 4, OBS-21); CSS defaults match the defaults (OBS-15); the spec file lists the asset. Behaviour of fade/limit/hidden parts (OBS-17–20, 23, 24) is checked by a JS-free reading of the code in review plus the manual check below. Run. Expected: FAIL.
2. Implement. 3. Verify, then full suite. 4. Commit `feat(overlay): chat page for OBS`.

- [x] 3.2 README section — verify: grep -q "Browser Sources" README.md

Covers: — · Test: none: documentation

Files: modify README.md

1. Add a Features bullet and a short "Chat on stream (OBS)" section: Settings → Browser Sources, tick the box, Copy the URL, OBS → Sources → + → Browser, paste, 400×600, transparent.
2. Commit `docs: chat on stream with OBS`.

## Manual check (before release)

`uv run chattersyn --demo` → Settings → Browser Sources → tick, OK → open the URL in a browser and in OBS (Browser Source 400×600): demo messages appear with icons, badges and emotes; change size/colours → OK → page restyles live; fade 5 s → rows fade; a demo delete removes the row; close and restart ChatterSyn → page keeps rows, then resumes.
