#!/usr/bin/env bash
# Commit everything, push to main, and cut a release tag.
#
# The commit message is the single positional argument. The release version is
# read from pyproject.toml ([project].version — the same field the CI "Stamp
# version" step uses) and tagged as `v<version>`. Pushing the tag triggers the
# build + GitHub release + apt-repo jobs.
#
# Every remote check runs BEFORE anything is committed, and git never falls
# back to a username/password prompt (GitHub rejects passwords for git). If a
# run dies after committing (e.g. a push failed), re-run it with no message:
# a clean tree whose HEAD is not on origin/main resumes with push + tag.
#
# Usage: scripts/release.sh "commit message"   (dirty tree: commit + release)
#        scripts/release.sh                    (clean tree: resume push + tag)
set -euo pipefail

# Fail fast instead of prompting for GitHub credentials.
export GIT_TERMINAL_PROMPT=0

if [ "$#" -gt 1 ]; then
    echo "usage: $0 [\"commit message\"]" >&2
    exit 2
fi
message="${1:-}"

branch="$(git branch --show-current)"
if [ "$branch" != "main" ]; then
    echo "error: not on main (current: ${branch:-detached}) — release from main" >&2
    exit 1
fi

dirty="$(git status --porcelain)"
if [ -n "$dirty" ] && [ -z "$message" ]; then
    echo "error: uncommitted changes — pass a commit message" >&2
    exit 2
fi

version="$(python3 - <<'PY'
import tomllib, pathlib
print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])
PY
)"
tag="v${version}"

# -- preflight: nothing is committed until all of these pass --------------------

if ! git fetch -q origin main; then
    echo "error: could not fetch origin/main — check the network / remote URL" >&2
    exit 1
fi
if ! git merge-base --is-ancestor origin/main HEAD; then
    echo "error: origin/main has commits that local main does not (e.g. a file" >&2
    echo "       edited on github.com). Integrate them first, then re-run:" >&2
    echo "         git pull --no-rebase origin main" >&2
    echo "       (merge, not rebase: released tags point at the local commits)" >&2
    exit 1
fi
if [ -z "$dirty" ] && [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
    echo "error: nothing to release (clean tree, HEAD already on origin/main)" >&2
    exit 1
fi
if ! git push -q --dry-run origin HEAD:refs/heads/main 2>/dev/null; then
    echo "error: git cannot authenticate to push to $(git remote get-url origin)" >&2
    echo "       GitHub does not accept passwords for git. One-time setup, either:" >&2
    echo "         SSH:  ssh-keygen -t ed25519, add ~/.ssh/id_ed25519.pub at" >&2
    echo "               github.com/settings/keys, then" >&2
    echo "               git remote set-url origin git@github.com:smitht06/red-chattersyn.git" >&2
    echo "         gh:   install the GitHub CLI, then gh auth login && gh auth setup-git" >&2
    exit 1
fi

tag_commit="$(git rev-parse -q --verify "refs/tags/${tag}^{commit}" 2>/dev/null || true)"
if [ -n "$tag_commit" ] && { [ -n "$dirty" ] || [ "$tag_commit" != "$(git rev-parse HEAD)" ]; }; then
    echo "error: tag ${tag} already exists — bump [project].version in pyproject.toml first" >&2
    exit 1
fi

# -- commit ----------------------------------------------------------------------

if [ -n "$dirty" ]; then
    # Keep the frozen-bundle version fallback in sync with [project].version so
    # the shipped binary shows the right version (a PyInstaller bundle has no
    # dist-info, so the app falls back to __version__ in src/chattersyn/__init__.py).
    python3 - "$version" <<'PY'
import pathlib, re, sys
version = sys.argv[1]
path = pathlib.Path("src/chattersyn/__init__.py")
text = path.read_text()
updated, n = re.subn(
    r'^__version__ = "[^"]*"$', f'__version__ = "{version}"', text, count=1, flags=re.M)
if n != 1:
    raise SystemExit("could not locate __version__ in src/chattersyn/__init__.py")
path.write_text(updated)
PY
    # CI installs with --locked: a lockfile still on the old version fails
    # every build leg.
    uv lock -q

    git add -A
    git commit -m "${message}"
else
    echo "resuming: releasing existing commit $(git rev-parse --short HEAD) as ${tag}"
fi

# -- publish ---------------------------------------------------------------------

git push origin HEAD:refs/heads/main
[ -n "$tag_commit" ] || git tag "${tag}"
git push origin "refs/tags/${tag}"

echo "Released ${tag} at $(git rev-parse --short HEAD)"
