# Releasing ChatterSyn

Maintainer notes, moved from the README. In Claude Code, `/release` does all of this for you.

Every push builds all three platforms; a pushed `v*` tag additionally runs a
`release` job that publishes a GitHub Release with the binaries attached
(macOS zip, Windows zip, Linux tar.gz + .deb), an `apt-repo` job that
publishes the Linux .deb into the APT repository, and a `homebrew` job that
publishes the macOS cask into the Homebrew tap (`smitht06/chattersyn`). The
GPG-signed `dists/stable` index is deployed to Cloudflare Pages; the .deb
itself stays a GitHub release asset (2 GB limit) and is reached via a Pages
redirect, since it exceeds GitHub's 100 MB git limit.

The easy way, in Claude Code, is `/release` (skill in
`.claude/skills/release/`). It defaults to the last tag + 0.0.1 and asks you
about the version, the files and dependency updates as it goes. It writes the
release notes (via `/release-notes`), bumps the version, gets your final
confirmation, runs `scripts/release.sh`, then tracks CI and checks that the
GitHub release, APT repo and Homebrew cask all updated
(`scripts/release_tools.py watch` / `verify`, no `gh` needed).

Release candidates: `/prerelease` (skill in `.claude/skills/prerelease/`)
cuts `vX.Y.ZrcN` (default: the next patch, `rc1`, `rc2`, …). CI marks it as a
GitHub **pre-release**, so "Latest" stays on the stable release. APT and
Homebrew users get it on their next upgrade. The `.deb` is versioned
`X.Y.Z~rcN` so the final `vX.Y.Z` (cut with `/release`) upgrades them. The
README's "Latest release" section stays on the latest stable release.

To cut a release by hand:

1. Bump `version` in `pyproject.toml`, then run `uv lock`. CI installs with
   `uv sync --locked`, and a stale lockfile fails every build leg — the
   release then contains only the auto-generated source archives, no binaries
   (this bit v0.2.0).
2. Commit and push, then tag the same commit and push the tag:

   ```sh
   git add pyproject.toml uv.lock && git commit -m "bump version to X.Y.Z"
   git push
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

   Artifact names are stamped from the `pyproject.toml` version at the tagged
   commit, so tag only after the bump lands on main.
3. Watch the Actions tab: `build` (all three OSes, up to ~45 min), then
   `release` (attaches the artifacts), `apt-repo` (updates the APT
   repository, so Linux users get the new version on their next
   `apt update && apt upgrade`), and `homebrew` (pushes the new cask, so
   macOS users get it on their next `brew upgrade --cask chattersyn`).
   Then write the release notes: in Claude Code, `/release-notes` (skill in
   `.claude/skills/release-notes/`) drafts them from the diff since the
   previous tag and refreshes the "Latest release" section at the bottom of
   this README; paste the same notes into the release page.

If a release comes out missing binaries: check the tag's workflow run
(`gh run list`). Every OS failing at `uv sync --locked` means the lockfile
wasn't regenerated before tagging. Fix by either moving the tag to a fixed
commit and re-pushing (`git tag -f vX.Y.Z && git push -f origin vX.Y.Z`) or
uploading the artifacts from a passing run:

```sh
gh run download <passing-run-id> --dir /tmp/rel
gh release upload vX.Y.Z /tmp/rel/*/*
```

The APT repository only updates from a passing `apt-repo` run, so it stays on
the previous version until the tag is fixed and re-pushed — `apt upgrade`
simply won't offer the broken release, which is the intended behavior. The
Homebrew tap works the same way: the cask stays on the previous version until
a passing `homebrew` run pushes it, and `brew upgrade --cask chattersyn`
keeps reporting the old release.
