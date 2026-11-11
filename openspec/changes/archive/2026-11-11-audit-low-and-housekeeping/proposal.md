## Why

The 2026-11-08 audit left five low-priority findings and some housekeeping: a
corrupt settings.json silently costs the user their tabs; a plaintext
secrets.json fallback outlives a keyring that works again; the Google sign-in
relay has no rate limit; CI runs third-party actions pinned only to tags with
broad default permissions; images are downloaded without a size cap. The repo
also carries a stale todo.md, an overlong local handoff, five test warnings, a
stale remote branch and a 2,000-line main_window.py.

## What Changes

- settings.json keeps a last-good backup and recovers from it, telling the user.
- A leftover secrets.json is moved into the keyring and deleted.
- Images over 8 MB are skipped.
- CI actions are pinned to commit SHAs, permissions are narrowed, Dependabot
  keeps the pins current; the relay gets a Cloudflare rate-limiting rule
  (documented, applied by the maintainer).
- Housekeeping: todo.md removed, handoff trimmed, test warnings fixed, stale
  branch deleted, two self-contained parts of main_window.py moved out, the
  README rewrite committed.

## Capabilities

### New Capabilities
- `local-data`: how ChatterSyn protects the settings, credentials and image data
  it keeps on the user's machine.

### Modified Capabilities

## Impact

config/settings.py, config/secrets.py, core/image_cache.py, app/main window
start-up messaging, .github/workflows, docs/, and a refactor of
ui/main_window.py. No new dependencies or permissions.
