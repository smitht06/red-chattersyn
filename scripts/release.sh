#!/usr/bin/env bash
# Commit everything, push to main, and cut a release tag.
#
# The commit message is the single positional argument. The release version is
# read from pyproject.toml ([project].version — the same field the CI "Stamp
# version" step uses) and tagged as `v<version>`. Pushing the tag triggers the
# build + GitHub release + apt-repo jobs.
#
# Usage: scripts/release.sh "commit message"
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: $0 \"commit message\"" >&2
    exit 2
fi
message="$1"

branch="$(git branch --show-current)"
if [ "$branch" != "main" ]; then
    echo "error: not on main (current: ${branch:-detached}) — release from main" >&2
    exit 1
fi

if [ -z "$(git status --porcelain)" ]; then
    echo "error: nothing to commit (working tree clean)" >&2
    exit 1
fi

version="$(python3 - <<'PY'
import tomllib, pathlib
print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])
PY
)"
tag="v${version}"

if git rev-parse -q --verify "refs/tags/${tag}" >/dev/null 2>&1; then
    echo "error: tag ${tag} already exists — bump [project].version in pyproject.toml first" >&2
    exit 1
fi

# Keep the frozen-bundle version fallback in sync with [project].version so the
# shipped binary shows the right version (a PyInstaller bundle has no dist-info,
# so the app falls back to __version__ in src/chattersyn/__init__.py).
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

git add -A
git commit -m "${message}"
git push
git tag "${tag}"
git push origin "${tag}"

echo "Released ${tag} at $(git rev-parse --short HEAD)"
