# Design: audit-low-and-housekeeping

## Context (facts checked 2026-11-10)

- `Settings.load` reads settings.json and, on JSON/OS/Unicode errors, logs and
  returns defaults; `save` writes `settings.json.tmp` and replaces the file, so
  the first save after a bad read overwrites it with defaults (tabs lost).
- `SecretsStore` uses the keyring and falls back to a 0600 `secrets.json`; once
  the keyring works again, `get` reads the keyring and never touches the file.
- `ImageCache._fetch` does `await client.get(url)` and keeps `response.content`
  whole; no size check.
- `.github/workflows/build.yml` uses actions/checkout@v4, download-artifact@v4,
  upload-artifact@v4, setup-python@v5, astral-sh/setup-uv@v5,
  cloudflare/wrangler-action@v4, flatpak-builder@v6, softprops/action-gh-release@v2,
  webfactory/ssh-agent@v0.10.0; only one job declares `permissions:`.
- The relay is a Cloudflare Pages Function (`site/functions/api/google/token.js`);
  Pages Functions have no built-in rate limiting; the zone's free plan includes
  one WAF rate-limiting rule.
- Test warnings: an un-awaited `ImageCache._fetch` (test_emote_picker,
  test_emote_providers), an unclosed event loop (test_emote_providers), an
  un-awaited `MainWindow._retry_identity` (test_main_window_moderation).

## Goals / Non-Goals

Goals: no silent loss of settings (D1); no plaintext token left behind (D2); a
size cap on images (D5); hardened CI and relay (D3, D4); a tidier repo (D6, D7).

Non-Goals: a full main_window.py split (D6); building "side-by-side chats".

## Open questions

None.

## Decision log

<!-- grillspec: depth=quick council=no budget=5 asked=7 -->

### Open

### Settled

- [x] D1 Corrupt settings — **keep `settings.json.bak` (previous good file) on every save; on an unreadable `settings.json` rename it to `settings.json.corrupt-<YYYYmmdd-HHMMSS>`, load the `.bak`, and tell the user "Your settings file was damaged — restored the last good copy."; if the `.bak` is bad too, defaults with a message; nothing is overwritten silently** · accepted · reversible: cheap
- [x] D2 Leftover secrets.json — **at startup, when the keyring works and secrets.json exists, move every entry into the keyring, then delete the file; if any move fails, the file stays as is** · accepted · reversible: cheap
- [x] D3 Relay rate limit — **a Cloudflare dashboard rate-limiting rule (free plan: 1 rule): 20 requests per 10 s per IP on /api/google/token, block 10 s; step-by-step instructions in docs/, applied by the user** · accepted · alternatives: KV/Durable Object counter in the function · reversible: cheap
- [x] D4 CI — **pin every third-party action to a commit SHA (version as a comment), top-level `permissions: contents: read` with write only where a job needs it, Dependabot for github-actions** · accepted · reversible: cheap
- [x] D5 Image cap — **skip any single image over 8 MB before decoding, and log it** · accepted · reversible: cheap
- [x] D6 main_window.py — **move the user-card code (enrichment, delete button) to ui/usercard_controller.py and the Twitch moderation-command runner to ui/twitch_commands.py, no behaviour change, existing tests unchanged; a full split is a later change** · accepted · reversible: cheap
- [x] D7 Housekeeping — **delete todo.md (note "toggle side-by-side chats" as an open idea in the CLAUDE.md handoff); trim the CLAUDE.md handoff to current state + last few releases; fix the 5 test warnings; delete remote branch twitch-usercard-follows; commit the README rewrite, screenshots, docs pages and skill edits with this change** · accepted · reversible: cheap (remote branch: costly — restorable from the local reflog/sha e7b392b)
- [x] D8 Fill-ins: the no-backup message text ("Your settings file was damaged and no backup could be read — started with default settings."); messages appear once at startup in the status bar and as a system row; a first launch shows nothing · agent's call · reversible: cheap
