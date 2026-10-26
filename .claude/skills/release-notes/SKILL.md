---
name: release-notes
description: Write user-facing release notes for a ChatterSyn version (default the next patch release, last v* tag + 0.0.1) from the real code diff, asking the developer clarifying questions along the way, then put them in the "Latest release" block at the bottom of README.md. Use when asked for release notes, a changelog, "what's in vX.Y.Z", or to update the README's latest release section. Also run by the /release skill.
argument-hint: "[vX.Y.Z]"
---

# Release notes

Produce release notes for one ChatterSyn release and keep README.md's
bottom "Latest release" block on the most recent release. Ask the developer
questions with the AskUserQuestion tool as you go (steps 1, 3 and 4). Keep
each question short and give 2–4 concrete options with the recommended one
first.

## 1. Pick the version and range

- Last release: `git tag --sort=-v:refname | head -1`.
- Default target: **last release + 0.0.1**
  (`uv run python scripts/release_tools.py next-version`). It's an upcoming
  release, so the range is `<last-tag>` → working tree (`git diff <last-tag>`
  with no second ref), and uncommitted work is included.
- If `$ARGUMENTS` names a version, use it (normalize to `vX.Y.Z`) and don't
  ask. If that version is already tagged, the range is `<tag before it>` →
  `<tag>`. Otherwise it's upcoming, as above. The /release skill always
  passes the version.
- If no version was given, ask which release to write notes for:
  - the default `vX.Y.(Z+1)` (Recommended);
  - `pyproject.toml`'s `[project].version`, if it's newer than the last tag
    and differs from the default (the developer may have bumped it already);
  - the latest existing tag, to redo its notes.
  - "Other" covers minor/major bumps.
- **Release candidates** (`vX.Y.ZrcN`, from the /prerelease skill): the
  range starts at the last **stable** tag, so each rc's notes cover
  everything since the last stable release. They aren't relative to the
  previous rc.
- Date: the tag's commit date (`git log -1 --format=%cs vX.Y.Z`) for a tagged
  release, today for an upcoming one.

## 2. Find out what actually changed

Commit messages here are mostly `bump version to X.Y.Z` and each one bundles
a whole release's work, so **don't build notes from `git log`**. Read the
diff:

1. `git diff --stat <range>` for the shape of the change, plus
   `git status --short` for new untracked files in an upcoming release.
2. Read the real diff of `src/` file by file, and new untracked source files
   in full. Read the tests too: new test names and docstrings often state the
   intended behaviour more clearly than the code.
3. If the local `CLAUDE.md` exists, its "Session handoff" lines describe the
   recent work in plain words. Use them to cross-check, never as the only
   source: only list what the diff shows.

Skip anything that isn't user-visible on its own: lockfile/version bumps,
test-only changes, lint, deleted stray files, CI plumbing, developer
scripts and skills. Mention a packaging or build change only if users or
packagers will notice it (a new install step, a changed minimum OS, a new
runtime dependency).

## 3. Ask about anything unclear

Before writing, ask about anything the diff can't settle. For example:
- Is a change user-facing, or was it internal or experimental?
- Is something half-finished and shouldn't be announced yet?
- Does a change need an Action needed step, such as a re-login or a
  changed setting?

Don't ask about what the diff already makes clear. Don't ask more than two
or three questions in one round. If nothing is unclear, skip this step.

## 4. Write the notes and get them approved

Write for the people running the app (streamers and mods), not for
developers. Use plain words: name the feature as it appears in the UI
("the user card", "Settings → General"), not class names or file paths.

Format: Markdown body with **no top-level heading** (the README script adds
it). Use these sections in this order, dropping empty ones:

- `### Action needed`: one-time steps users must take, such as "Reconnect
  your Twitch account once to grant the new moderation permission". Always
  first when present. Look for OAuth scope changes (`SCOPES` in
  `services/twitch/auth.py`, YouTube scopes), settings migrations and new
  required credentials.
- `### New`: features.
- `### Improved`: visible changes to existing features.
- `### Fixed`: bugs users could hit, described by the symptom ("Twitch
  bans and timeouts didn't work"), with the fix in a few words.

Make each item one bullet, one or two sentences. Lead with a **bold**
two-to-four-word name. Don't add marketing language, emoji or credit lines.

Show the full draft in your message, then ask: "Use these release notes?"
with the options "Yes, use them" (Recommended) and "Change something"
(their text says what). Revise and ask again until they approve. **Don't
touch the README before approval.**

## 5. Update the README

**Skip this step for a release candidate.** The README's "Latest release"
block always shows the latest *stable* release, and the script refuses rc
versions anyway. Go straight to step 6.

1. Write the approved body to a scratch file (your scratchpad directory, not
   the repo).
2. Run
   `uv run python scripts/update_readme_release.py vX.Y.Z YYYY-MM-DD <notes-file>`.
   It replaces the block between `<!-- release-notes:start -->` and
   `<!-- release-notes:end -->` (or appends it at the end the first time), so
   the README only ever shows the latest release.
3. Run `git diff README.md` to confirm that only the block changed.

## 6. Report

Paste the notes, ready to copy into the GitHub release page (Releases → the
tag → Edit). Say whether the version is released or upcoming. Don't run
`gh release edit` or push, because that publishes the notes. Don't commit
either: the /release skill or the developer does that.
