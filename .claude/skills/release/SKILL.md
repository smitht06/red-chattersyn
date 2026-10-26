---
name: release
description: Cut a ChatterSyn release end to end. Pick the version (default last v* tag + 0.0.1), review what goes in, optionally update dependencies, write release notes, bump the package version, commit, push and tag via scripts/release.sh, then track the CI/CD run and check that the GitHub release, APT repo and Homebrew cask all updated. Asks the developer clarifying questions throughout and gets explicit final confirmation before anything is pushed. Use when asked to release, ship, cut or publish a new version.
argument-hint: "[vX.Y.Z | patch | minor | major]"
---

# Release

Ship one ChatterSyn version. **Nothing leaves the machine until the developer
confirms in step 8.** Up to then every step is local and reversible.

Ask questions with the AskUserQuestion tool: short questions, 2–4 concrete
options, recommended option first. If an answer is "Other", act on its text.
If the developer says stop at any point, stop and summarize what changed
locally.

Repo rules that apply here (from the project's working rules):
- Commit messages get **no `Co-Authored-By` trailer** and keep the
  configured git identity.
- Secrets (tokens, `client_secrets*.json`, `.env`, keyring exports) are never
  committed.
- Network commands run unsandboxed.

## 1. Preflight (no questions yet)

Run these, and stop with a clear explanation if one fails:
- `git branch --show-current` is `main`.
- `git fetch -q origin main` and `git merge-base --is-ancestor origin/main HEAD`.
  If GitHub has commits that local main doesn't, ask before running
  `git pull --no-rebase origin main` (merge, not rebase: released tags point
  at local commits).
- `uv run ruff check src/ tests/` and `uv run pytest -q` both pass. Never
  release on red: show the failures and ask whether to fix them first.

## 2. Pick the version

- Default: `uv run python scripts/release_tools.py next-version` (last tag
  + 0.0.1).
- If `$ARGUMENTS` is `patch`, `minor` or `major`, run
  `next-version <part>`. If it's a version, use it. In both cases skip
  the question unless the version is already tagged or isn't newer than
  the last tag.
- Otherwise ask "Which version?" with:
  - `vX.Y.(Z+1)` patch (Recommended);
  - `vX.(Y+1).0` minor;
  - `pyproject.toml`'s version, if it's newer than the last tag and isn't
    already one of the options above.
  - "Other" covers major or a custom version.
- If the release adds features (not just fixes), mention that a minor bump
  may fit better, but default to patch as asked.

## 3. Review what goes into the release

`scripts/release.sh` commits **everything** (`git add -A`). So list
`git status --short` (with untracked files) and
`git diff --stat <last-tag>`, grouped as source / tests / docs /
packaging / other.

Flag anything that looks unintended: scratch or notes files (`todo.md`),
large binaries, build output, editor files, and anything that could hold
secrets (never commit those). Then ask "Include all of these changes?"
with these options:
- "Include everything" (Recommended, unless something was flagged).
- "Exclude some files". Ask which (multiSelect for up to 4 of them, or let
  them type paths), then park them with
  `git stash push --include-untracked -m release-excluded -- <paths>`.
  Remember to restore them in step 10.

## 4. Packages and dependencies

The package version bump (pyproject.toml, `uv.lock`, `__version__`) always
happens: step 6 and `release.sh` handle it. Ask "Update dependencies in
this release?" with these options:
- "No, keep the lockfile" (Recommended).
- "Upgrade everything": `uv lock --upgrade`.
- "Upgrade specific packages": `uv lock --upgrade-package <name>` for each
  package they name.

After any upgrade, run `uv sync`, then lint and the full test suite again,
and show `git diff --stat uv.lock`. If tests fail, ask whether to fix, roll
back the upgrade (`git checkout uv.lock && uv sync`) or stop.

## 5. Release notes

Run the release-notes skill with the chosen version (Skill tool,
`release-notes`, args `vX.Y.Z`). It reads the diff, asks its own questions,
gets the notes approved and updates the README's "Latest release" block.
Keep the approved notes for steps 8 and 11.

## 6. Bump the version

`uv run python scripts/release_tools.py set-version X.Y.Z`. `release.sh`
then syncs `src/chattersyn/__init__.py`'s `__version__` and runs `uv lock`
before committing, because CI installs with `--locked` and a stale lockfile
fails every build. Run `git diff pyproject.toml` to check the change.

## 7. Commit message

Propose `release vX.Y.Z: <a few words from the notes' headline items>`
(single line, no trailers). Ask "Commit message?" with the options
"Use: <proposed>" (Recommended) and "Edit it" (they type the new text).

## 8. Final confirmation

Show one summary:
- **Version:** `vX.Y.Z` (previous `vA.B.C`).
- **Commit:** the message, plus the files that will be committed
  (`git status --short`) and any parked files.
- **Dependencies:** unchanged, or what was upgraded.
- **Release notes:** the approved notes.
- **What happens next:** push `main`, then push tag `vX.Y.Z`. That starts
  CI: `build` on 3 OSes (up to ~45 min), then `release` (the GitHub release
  with binaries), then `apt-repo` + `homebrew` (Linux and macOS users get
  the update). A pushed tag can't be quietly taken back.

Then ask "Release vX.Y.Z now?" with the options "Yes, release it" and
"No, stop here". Don't mark either as recommended. Only "Yes" continues. On
"No", stop: leave the version bump and notes in place and say how to resume
(run `/release` again).

## 9. Release

Run `scripts/release.sh "<commit message>"` unsandboxed. It checks push
access and divergence before committing, then commits, pushes main, tags and
pushes the tag.
- If the tool call is denied (Claude Code permissions or auto mode block
  publishing actions), don't retry it or work around it. Ask the developer
  to run it themselves by typing `! scripts/release.sh "<commit message>"`
  in the prompt, then continue from step 10 once it's done.
- If `release.sh` fails, show its error. Fix the cause only with the
  developer's OK (for example GitHub auth setup, or a pull for divergence).
  If it failed after committing, the resume form is `scripts/release.sh`
  with no message.

## 10. Restore parked files

If step 3 parked files, run `git stash pop` (the `release-excluded` entry)
and confirm they're back.

## 11. Track CI/CD

1. Start
   `uv run python scripts/release_tools.py watch vX.Y.Z`
   unsandboxed with `run_in_background`. It polls GitHub every 3 minutes
   (no `gh` needed), prints each job state change, and exits 0 on success,
   1 on failure, 3 on timeout. Tell the developer it's running and that the
   build takes up to ~45 min. Don't poll it yourself: you're notified when
   it exits. If they ask in the meantime, read its output.
2. **Success:** run
   `uv run python scripts/release_tools.py verify X.Y.Z`. The APT index
   (Cloudflare Pages) and the Homebrew tap (raw GitHub) can lag a few
   minutes behind CI. If only those two lag, re-check in the background
   (`for i in 1 2 3 4 5; do uv run python scripts/release_tools.py verify X.Y.Z && break; sleep 120; done`
   with `run_in_background`).
3. **Failure:** open the run URL it printed, name the failed job(s), and
   check the README's "Releasing" section for known causes (for example
   every OS failing at `uv sync --locked` means a stale lockfile). Explain
   the fix and **ask before** anything that rewrites a published tag
   (`git tag -f` / `git push -f`) or re-runs CI.

## 12. Wrap up

Report the version, the commit, the CI result per job, and where it landed
(GitHub release assets, APT version, Homebrew cask version). Paste the
approved notes for the GitHub release page (Releases → `vX.Y.Z` → Edit),
since this machine has no `gh` to set them. If the local `CLAUDE.md`
exists, add the release to its "Session handoff" section.
