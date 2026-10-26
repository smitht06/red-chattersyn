#!/usr/bin/env python3
"""Release helpers for the /release and /release-notes skills.

Usage::

    release_tools.py next-version [patch|minor|major]   # last v* tag, bumped
    release_tools.py set-version X.Y.Z                  # pyproject [project].version
    release_tools.py watch vX.Y.Z [--interval S] [--timeout S]
    release_tools.py verify X.Y.Z

``watch`` follows the GitHub Actions run of a pushed tag (build → release →
apt-repo + homebrew) until it finishes. It prints each job state change and
exits 0 on success, 1 on failure, 3 on timeout. ``verify`` checks that the
version actually landed everywhere users get it: the GitHub release assets,
the APT index and the Homebrew cask. It exits 0 when all three match.

Everything reads public endpoints, so no ``gh`` CLI is needed. Unauthenticated
GitHub API calls are capped at 60/hour, so ``watch`` polls slowly (default
every 180 s, two calls per poll). Set GITHUB_TOKEN to raise the cap.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_API = "https://api.github.com/repos/smitht06/red-chattersyn"
APT_PACKAGES = "https://chattersyn-apt.pages.dev/dists/stable/main/binary-amd64/Packages"
CASK = "https://raw.githubusercontent.com/smitht06/homebrew-chattersyn/main/Casks/chattersyn.rb"
# Artifact suffixes the release job attaches (chattersyn-<version>-<suffix>).
EXPECTED_ASSETS = (
    "linux-x86_64.deb", "linux-x86_64.tar.gz", "macos-arm64.zip", "windows-x86_64.zip")

_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


# -- versions ------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a version: {text!r} (expected X.Y.Z or vX.Y.Z)")
    return int(match[1]), int(match[2]), int(match[3])


def bump(version: str, part: str = "patch") -> str:
    """'0.6.0' -> '0.6.1' (patch) / '0.7.0' (minor) / '1.0.0' (major)."""
    major, minor, patch = parse_version(version)
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "major":
        return f"{major + 1}.0.0"
    raise ValueError(f"unknown bump part: {part!r}")


def latest_tag(tags: list[str]) -> str | None:
    """Highest vX.Y.Z tag by version order (ignores anything else)."""
    versions = [tag for tag in tags if _VERSION_RE.fullmatch(tag) and tag.startswith("v")]
    return max(versions, key=parse_version, default=None)


def git_tags() -> list[str]:
    out = subprocess.run(["git", "tag", "--list", "v*"], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout
    return out.split()


def set_pyproject_version(pyproject: str, version: str) -> str:
    """`pyproject` text with [project].version replaced (first `version =`
    line inside the [project] table only)."""
    parse_version(version)
    match = re.search(r'(?ms)^\[project\]\s*$.*?^version\s*=\s*"([^"]*)"', pyproject)
    if match is None:
        raise ValueError("no [project] version in pyproject.toml")
    start, end = match.span(1)
    return pyproject[:start] + version + pyproject[end:]


# -- GitHub Actions -------------------------------------------------------------------


def http_get(url: str) -> bytes:
    headers = {"User-Agent": "chattersyn-release-tools", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
        return resp.read()


def get_json(url: str):
    return json.loads(http_get(url))


def find_tag_run(runs: list[dict], tag: str) -> dict | None:
    """Newest push-triggered run for `tag` (a tag push's head_branch is the tag)."""
    for run in runs:
        if run.get("head_branch") == tag and run.get("event") == "push":
            return run
    return None


def run_outcome(run: dict) -> str:
    """'pending' until the run completes, then 'success' or 'failure'."""
    if run.get("status") != "completed":
        return "pending"
    return "success" if run.get("conclusion") == "success" else "failure"


def job_lines(jobs: list[dict]) -> list[str]:
    lines = []
    for job in jobs:
        state = job.get("conclusion") or job.get("status") or "?"
        lines.append(f"  {job.get('name', '?')}: {state}")
    return lines


def watch(tag: str, *, interval: float = 180, timeout: float = 4 * 3600,
          fetch: Callable[[str], object] = get_json,
          sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic,
          out=print) -> int:
    deadline = clock() + timeout
    last: list[str] | None = None
    while True:
        try:
            runs = fetch(f"{REPO_API}/actions/runs?event=push&per_page=20")["workflow_runs"]
            run = find_tag_run(runs, tag)
            if run is None:
                lines = [f"{tag}: no CI run yet (waiting for GitHub to start it)"]
            else:
                jobs = fetch(f"{REPO_API}/actions/runs/{run['id']}/jobs")["jobs"]
                lines = [f"{tag}: run {run['id']} {run_outcome(run)} — {run.get('html_url', '')}"]
                lines += job_lines(jobs)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
            run, lines = None, [f"{tag}: GitHub API error ({exc}); retrying"]
        if lines != last:
            out("\n".join(lines))
            last = lines
        if run is not None and run_outcome(run) != "pending":
            return 0 if run_outcome(run) == "success" else 1
        if clock() >= deadline:
            out(f"{tag}: timed out waiting for CI")
            return 3
        sleep(interval)


# -- published packages -----------------------------------------------------------------


def apt_version(packages: str) -> str | None:
    for stanza in packages.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in stanza.splitlines() if ": " in line)
        if fields.get("Package") == "chattersyn":
            return fields.get("Version")
    return None


def cask_version(cask: str) -> str | None:
    match = re.search(r'^\s*version\s+"([^"]+)"', cask, re.M)
    return match[1] if match else None


def missing_assets(release: dict, version: str) -> list[str]:
    names = {asset.get("name") for asset in release.get("assets", [])}
    return [f"chattersyn-{version}-{suffix}" for suffix in EXPECTED_ASSETS
            if f"chattersyn-{version}-{suffix}" not in names]


def verify(version: str, *, fetch_json: Callable[[str], object] = get_json,
           fetch_text: Callable[[str], str] = lambda url: http_get(url).decode()) -> list[str]:
    """Problems found (empty = the release is fully published)."""
    problems: list[str] = []
    try:
        release = fetch_json(f"{REPO_API}/releases/tags/v{version}")
        missing = missing_assets(release, version)
        if missing:
            problems.append(f"GitHub release v{version} is missing: {', '.join(missing)}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        problems.append(f"GitHub release v{version} not found ({exc})")
    for label, url, parse in (("APT index", APT_PACKAGES, apt_version),
                              ("Homebrew cask", CASK, cask_version)):
        try:
            found = parse(fetch_text(url))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            problems.append(f"{label}: fetch failed ({exc})")
            continue
        if found != version:
            problems.append(f"{label} is at {found or 'unknown'}, expected {version}")
    return problems


# -- CLI -------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    nv = sub.add_parser("next-version")
    nv.add_argument("part", nargs="?", default="patch", choices=("patch", "minor", "major"))
    sv = sub.add_parser("set-version")
    sv.add_argument("version")
    wa = sub.add_parser("watch")
    wa.add_argument("tag")
    wa.add_argument("--interval", type=float, default=180)
    wa.add_argument("--timeout", type=float, default=4 * 3600)
    ve = sub.add_parser("verify")
    ve.add_argument("version")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "next-version":
            tag = latest_tag(git_tags())
            print("v" + bump(tag or "v0.0.0", args.part))
            return 0
        if args.cmd == "set-version":
            path = ROOT / "pyproject.toml"
            version = args.version.removeprefix("v")
            path.write_text(set_pyproject_version(path.read_text(), version))
            print(f"pyproject.toml version -> {version}")
            return 0
        if args.cmd == "watch":
            return watch(args.tag if args.tag.startswith("v") else f"v{args.tag}",
                         interval=args.interval, timeout=args.timeout)
        problems = verify(args.version.removeprefix("v"))
        for problem in problems:
            print(f"- {problem}")
        print("all published" if not problems else "not fully published yet")
        return 0 if not problems else 1
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
