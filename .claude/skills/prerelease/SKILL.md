---
name: prerelease
description: Cut a ChatterSyn release candidate (vX.Y.ZrcN, default the next patch rc after the last stable release). It's published as a GitHub pre-release AND to the APT repo and Homebrew tap, so testers on apt/brew get it. Asks the developer clarifying questions throughout, writes rc release notes, bumps the version, gets explicit final confirmation, runs scripts/release.sh, then tracks CI and checks every channel. Use when asked for a pre-release, release candidate, rc, beta or test build.
argument-hint: "[vX.Y.ZrcN | patch | minor | major]"
---

# Pre-release (release candidate)

Ship one release candidate. **Nothing leaves the machine until the developer
confirms in step 8.** Up to then every step is local and reversible.

How an rc reaches people:
- **GitHub:** marked as a *pre-release*, so the repo's "Latest" stays on the
  last stable release. This happens in CI because the tag contains `rc`.
- **APT and Homebrew:** published like a normal release, so **everyone who
  installed via apt or brew gets it on their next upgrade.** Say this
  plainly in step 8.
- **Moving to the final release:** the APT package is versioned
  `X.Y.Z~rcN` (`scripts/build_deb.py: debian_version`), which sorts before
  `X.Y.Z`, so the final release upgrades rc users. Homebrew treats any
  version change as an upgrade.

Ask questions with the AskUserQuestion tool: short questions, 2–4 concrete
options, recommended option first. If an answer is "Other", act on its text.
If the developer says stop at any point, stop and summarize what changed
locally.

Repo rules: commit messages get **no `Co-Authored-By` trailer** and keep the
configured git identity. Secrets are never committed. Network commands run
unsandboxed.

## 1. Preflight (no questions yet)

Same as the release skill. Stop with a clear explanation on any failure:
- `git branch --show-current` is `main`.
- `git fetch -q origin main` and `git merge-base --is-ancestor origin/main HEAD`.
  If GitHub is ahead, ask before `git pull --no-rebase origin main`.
- `uv run ruff check src/ tests/` and `uv run pytest -q` both pass. Never
  ship on red.

## 2. Pick the version

- Default: `uv run python scripts/release_tools.py next-prerelease`. That's
  the last stable tag + 0.0.1 with `rc1`, or the next rc number if that
  version already has rcs (v0.7.1rc1, then v0.7.1rc2, …).
- An argument of `patch`, `minor` or `major` becomes
  `next-prerelease <part>`. A full `vX.Y.ZrcN` is used as given, unless
  it's already tagged. Skip the question in both cases.
- Otherwise ask "Which release candidate?" with:
  - the patch rc (Recommended);
  - the minor rc (`next-prerelease minor`);
  - "Other" for major or a custom rc.
- If an rc for a *different* upcoming version already exists (for example
  `v0.8.0rc1` while the default is `v0.7.1rc1`), offer continuing that one
  (`v0.8.0rc2`) as the first option instead.

## 3. Review what goes in

`scripts/release.sh` commits **everything** (`git add -A`). Show
`git status --short` (with untracked files) and
`git diff --stat <last-stable-tag>`, grouped as source / tests / docs /
packaging / other.

Flag scratch files (`todo.md`), build output, large binaries and anything
that could hold secrets. Then ask "Include all of these changes?" with:
- "Include everything" (Recommended unless something was flagged);
- "Exclude some files": park them with
  `git stash push --include-untracked -m prerelease-excluded -- <paths>`
  and restore them in step 10.

Dependencies stay as they are. Don't offer upgrades for an rc: testers
should be testing the same dependencies the final release will ship.

## 4. What should testers focus on?

Ask "Anything specific testers should try?" with:
- "No, just the release notes" (Recommended);
- "Yes", with what to test given in their own text.

Their answer becomes a `### Please test` section at the top of the notes.

## 5. Release notes

Run the release-notes skill with the rc version (Skill tool,
`release-notes`, args `vX.Y.ZrcN`). For an rc it covers everything since
the last **stable** release, gets the notes approved, and **doesn't touch
the README**, which keeps showing the latest stable release. Put the
step 4 section first. Keep the approved notes for steps 8 and 12.

## 6. Bump the version

`uv run python scripts/release_tools.py set-version X.Y.ZrcN` (PEP 440
form, no dash). `release.sh` then syncs `__version__` and runs `uv lock`
before committing. Check with `git diff pyproject.toml`.

## 7. Commit message

Propose `pre-release vX.Y.ZrcN: <a few words from the notes>` (single
line, no trailers). Ask "Commit message?" with the options
"Use: <proposed>" (Recommended) and "Edit it".

## 8. Final confirmation

Show one summary:
- **Version:** `vX.Y.ZrcN` (last stable `vA.B.C`).
- **Commit:** the message, the files (`git status --short`) and any parked
  files.
- **Release notes:** the approved notes.
- **Who gets it:**
  - a GitHub **pre-release** (not "Latest");
  - **every APT and Homebrew user on their next upgrade** (APT as
    `X.Y.Z~rcN`);
  - the final `vX.Y.Z` later upgrades them all.
- **What happens next:** push `main`, then push tag `vX.Y.ZrcN`. CI runs
  `build` on 3 OSes (up to ~45 min), then `release`, `apt-repo` and
  `homebrew`. A pushed tag can't be quietly taken back.

Ask "Publish pre-release vX.Y.ZrcN now?" with the options "Yes, publish it"
and "No, stop here", with neither marked as recommended. Only "Yes"
continues. On "No", leave the version bump in place and say how to resume
(`/prerelease` again).

## 9. Publish

Run `scripts/release.sh "<commit message>"` unsandboxed. It tags
`v<pyproject version>`, which is `vX.Y.ZrcN`.
- If the tool call is denied (permissions or auto mode), don't retry or
  work around it. Ask the developer to type
  `! scripts/release.sh "<commit message>"` and continue from step 10 when
  it's done.
- If `release.sh` fails, show the error and fix the cause only with the
  developer's OK. If it failed after committing, resume with
  `scripts/release.sh` (no message).

## 10. Restore parked files

If step 3 parked files, run `git stash pop` (the `prerelease-excluded`
entry) and confirm they're back.

## 11. Track CI/CD and verify

1. Start `uv run python scripts/release_tools.py watch vX.Y.ZrcN` unsandboxed
   with `run_in_background`. Tell the developer it's running (up to ~45 min)
   and wait for the notification instead of polling.
2. **Success:** run `uv run python scripts/release_tools.py verify X.Y.ZrcN`.
   For an rc it checks four things:
   - the 4 assets;
   - that the GitHub release **is marked pre-release**;
   - APT at `X.Y.Z~rcN`;
   - the cask at `X.Y.ZrcN`.
   APT and the tap can lag a few minutes. If only they lag, re-check in the
   background
   (`for i in 1 2 3 4 5; do uv run python scripts/release_tools.py verify X.Y.ZrcN && break; sleep 120; done`).
3. **Failure:** name the failed job(s) from the run URL, check the README's
   "Releasing" section for known causes, and **ask before** rewriting a
   published tag or re-running CI.

## 12. Wrap up

Report the version, the commit, CI per job, and where it landed. Paste the
approved notes for its GitHub release page (Releases → `vX.Y.ZrcN` → Edit).
Then explain the next step:
- **Another rc:** run `/prerelease` again (defaults to `rcN+1`).
- **Final release:** run `/release`. It defaults to the last stable tag +
  0.0.1, which is this rc's `X.Y.Z`. Its notes and README block then cover
  the whole release.

If the local `CLAUDE.md` exists, add the pre-release to its "Session handoff"
section.
