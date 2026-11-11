# audit-low-and-housekeeping — plan

## Goal

Close the audit's low-priority findings (settings backup, plaintext secrets,
image cap, CI, relay) and the housekeeping list, with no behaviour change beyond
the spec.

## Architecture

`Settings` gains a `.bak` on save and a recovery path in `load` that records a
`load_problem` message; MainWindow shows it at start. `SecretsStore` gains
`migrate_file_to_keyring()`, called once by MainWindow at start. `ImageCache`
streams downloads and stops past 8 MB. CI and the relay are config and docs. Two
blocks of `main_window.py` move into helper modules behind the same MainWindow
methods.

**Spec:** openspec/changes/audit-low-and-housekeeping/specs/local-data/spec.md

## Global Constraints

- Commit or push only when the user asks; never add a `Co-Authored-By` trailer (CLAUDE.md). The user approved (D7) deleting the remote branch `twitch-usercard-follows` and committing this change's work.
- Tests need no credentials and no network; the keyring is faked in tests (never touch the real OS keyring).
- Exact texts: "Your settings file was damaged — restored the last good copy."; "Your settings file was damaged and no backup could be read — started with default settings."; file names `settings.json.bak`, `settings.json.corrupt-<YYYYmmdd-HHMMSS>`; cap `8 * 1024 * 1024` bytes.
- The refactor (3.1) changes no behaviour: existing tests pass without edits other than import paths.
- `uv run ruff check src/ tests/` and `uv run pytest -q` stay green after every task.

## Review Focus

1. A failed save must never leave settings.json missing: the `.bak` copy happens before the atomic replace, and a crash between the two leaves the old file in place → task 1.1 pins `test_bak_written_before_replace`.
2. Secrets migration must not delete the file when the keyring "accepts" a write it can't read back → task 1.2 pins `test_migration_verifies_by_reading_back`.
3. The image cap must hold for responses without Content-Length (chunked) → task 1.3 pins `test_cap_without_content_length`.

## 1. Local data

- [x] 1.1 Settings backup and recovery — verify: uv run pytest tests/test_settings.py tests/test_main_window_settings.py -q passes

Covers: LD-1, LD-2, LD-3, LD-4 · Test: scenario-first

Files: modify src/chattersyn/config/settings.py, src/chattersyn/ui/main_window.py; test tests/test_settings.py, tests/test_main_window_settings.py

Interfaces:
- `Settings.load(config_dir, *, now=None)`: on an unreadable settings.json, rename it to `settings.json.corrupt-<YYYYmmdd-HHMMSS>` (local time from `now()`), try `settings.json.bak`; sets `settings.load_problem: str` ("" when none) to one of the two exact texts.
- `Settings.save()`: copies the current settings.json to `settings.json.bak` (only if it exists and parses) before the existing tmp-write + replace.
- MainWindow (not demo): when `settings.load_problem`, show it in the status bar and as one system row in the first tab at start.

1. Write tests: `test_save_keeps_previous_as_bak` (LD-1), `test_corrupt_restored_from_bak` (LD-2: tabs from the bak, corrupt file renamed with the injected time, message), `test_corrupt_without_bak_uses_defaults` (LD-3), `test_first_launch_no_message` (LD-4), `test_bak_written_before_replace` (Review Focus 1: make the replace raise → settings.json unchanged and .bak present), `test_damaged_bak_never_replaces_good_file`, and in tests/test_main_window_settings.py `test_load_problem_shown_at_start`. Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `fix(settings): keep a backup and recover from a damaged settings file`.

- [x] 1.2 Move leftover plaintext credentials into the keyring — verify: uv run pytest tests/test_secrets.py -q passes

Covers: LD-5, LD-6 · Test: scenario-first

Files: modify src/chattersyn/config/secrets.py, src/chattersyn/ui/main_window.py; test tests/test_secrets.py

Interfaces:
- `SecretsStore.migrate_file_to_keyring() -> bool`: no keyring or no file → False; else set every file entry in the keyring and read each back; all equal → delete the file and return True; any failure → leave the file untouched, return False (log why).
- MainWindow calls it once right after creating the store (not demo).

1. Write tests with a fake keyring: `test_file_moved_into_working_keyring` (LD-5), `test_refused_entry_keeps_file` (LD-6: byte-identical file), `test_migration_verifies_by_reading_back` (Review Focus 2), `test_no_file_no_change`. Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `fix(secrets): move a leftover plaintext secrets.json into the keyring`.

- [x] 1.3 Image size cap — verify: uv run pytest tests/test_image_cache.py -q passes

Covers: LD-7, LD-8 · Test: scenario-first

Files: modify src/chattersyn/core/image_cache.py; test tests/test_image_cache.py

Interfaces:
- `MAX_IMAGE_BYTES = 8 * 1024 * 1024` in image_cache.py; `_fetch` streams the body (`client.stream("GET", url)`), refuses when Content-Length exceeds the cap or when the bytes read pass it, logs `image too large (> 8 MB): <url>`, and emits nothing for that URL.

1. Write tests (respx): `test_oversized_image_skipped` (LD-7: 9 MB body, no decode, log names the URL), `test_normal_image_loads` (LD-8), `test_cap_without_content_length` (Review Focus 3: streamed chunks without the header). Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `fix(images): skip downloads over 8 MB`.

## 2. CI and relay

- [x] 2.1 Pin actions, narrow permissions, add Dependabot — verify: uv run python -c "import yaml,glob;[yaml.safe_load(open(f)) for f in glob.glob('.github/**/*.yml',recursive=True)]" passes

Covers: — · Test: none: workflow configuration; proven by the next CI run

Files: modify .github/workflows/build.yml; create .github/dependabot.yml

1. Resolve each action tag to its commit SHA with `git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>` (dereferenced `^{}` when annotated); replace `uses: x@vN` with `uses: x@<sha> # vN`.
2. Add top-level `permissions: contents: read`; give `contents: write` only to the jobs that need it (release; any other job that pushes to this repo), keeping the existing release-job block.
3. Create `.github/dependabot.yml` for `github-actions`, weekly.
4. Run the verify command. Expected: no error.
5. Commit `ci: pin actions to commit SHAs, least-privilege permissions, dependabot`.

- [x] 2.2 Relay rate-limit instructions — verify: test -f docs/relay-rate-limit.md

Covers: — · Test: none: documentation of a Cloudflare dashboard setting

Files: create docs/relay-rate-limit.md; modify docs/advanced-setup.md (link)

1. Write click-by-click steps for a WAF rate-limiting rule on the redcyberdragon.net zone: match `http.request.uri.path eq "/api/google/token"`, 20 requests per 10 s per IP, action Block for 10 s; how to check it works.
2. Commit `docs: rate-limit rule for the sign-in relay`.

## 3. Housekeeping

- [x] 3.1 Move user-card and Twitch-command code out of main_window.py — verify: uv run pytest -q passes

Covers: — · Test: characterization

The existing user-card and Twitch-command tests pin the behaviour and must pass unchanged.

Files: create src/chattersyn/ui/usercard_controller.py, src/chattersyn/ui/twitch_commands.py; modify src/chattersyn/ui/main_window.py

1. Move the user-card enrichment helpers (`_enrich_*usercard`, `_usercard_follower_total`, `_usercard_follow_date`) and the Twitch Helix command runner (`_twitch_helix_command`, `_show_user_list`) into the two modules as functions taking the window; keep MainWindow methods of the same names as thin delegates so callers and tests don't change.
2. Run `uv run ruff check src/ tests/` and `uv run pytest -q`. Expected: PASS with no test edits.
3. Commit `refactor(ui): move user-card and Twitch-command code out of main_window`.

- [x] 3.2 Test warnings, todo.md, README work — verify: uv run pytest -q -W error::RuntimeWarning -W error::ResourceWarning -W error::DeprecationWarning passes

Covers: — · Test: none: test-hygiene and repo cleanup; the verify command proves the warnings are gone

Files: modify tests/test_emote_picker.py, tests/test_emote_providers.py, tests/test_main_window_moderation.py (and src only if a warning comes from product code); delete todo.md; add README.md, docs/, .claude/skills/release/SKILL.md, .claude/skills/prerelease/SKILL.md

1. Fix the un-awaited `ImageCache._fetch` / unclosed loop / un-awaited `_retry_identity` warnings at their source.
2. Delete todo.md.
3. Run the verify command. Expected: PASS, no warnings.
4. Commit `chore: clean test warnings, drop todo.md, README rewrite with screenshots`.
