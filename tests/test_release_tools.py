"""Release helpers: version bumps, CI watch loop, published-package checks
(all network calls faked)."""

import pytest
from release_tools import (
    REPO_API,
    apt_version,
    bump,
    cask_version,
    latest_tag,
    missing_assets,
    set_pyproject_version,
    verify,
    watch,
)

# -- versions -------------------------------------------------------------------------


def test_bump_defaults_to_patch():
    assert bump("0.6.0") == "0.6.1"
    assert bump("v0.6.9", "patch") == "0.6.10"
    assert bump("0.6.3", "minor") == "0.7.0"
    assert bump("0.6.3", "major") == "1.0.0"
    with pytest.raises(ValueError):
        bump("0.6")


def test_latest_tag_orders_by_version_not_text():
    tags = ["v0.5.3", "v0.10.0", "v0.9.1", "v0.3.2 2", "nightly"]
    assert latest_tag(tags) == "v0.10.0"
    assert latest_tag([]) is None


def test_set_pyproject_version_only_touches_project_table():
    text = ('[build-system]\nrequires = ["x"]\n\n[project]\nname = "chattersyn"\n'
            'version = "0.6.0"\n\n[tool.other]\nversion = "9.9.9"\n')
    out = set_pyproject_version(text, "0.6.1")
    assert 'version = "0.6.1"' in out and 'version = "9.9.9"' in out
    assert out.count('"0.6.0"') == 0
    with pytest.raises(ValueError):
        set_pyproject_version(text, "nope")


# -- CI watch ---------------------------------------------------------------------------


def _fake_ci(states):
    """fetch() that walks the tag run through `states` (one per runs poll)."""
    calls = {"n": 0}

    def fetch(url):
        if "/jobs" in url:
            status, conclusion = states[min(calls["n"], len(states)) - 1]
            return {"jobs": [{"name": "build (ubuntu-latest)", "status": status,
                              "conclusion": conclusion}]}
        calls["n"] += 1
        status, conclusion = states[min(calls["n"], len(states)) - 1]
        return {"workflow_runs": [
            {"id": 1, "head_branch": "main", "event": "push", "status": "completed",
             "conclusion": "success"},
            {"id": 7, "head_branch": "v0.6.1", "event": "push", "status": status,
             "conclusion": conclusion, "html_url": "https://example/run/7"},
        ]}
    return fetch


def test_watch_follows_tag_run_until_success():
    out: list[str] = []
    code = watch("v0.6.1", fetch=_fake_ci([("in_progress", None), ("completed", "success")]),
                 sleep=lambda s: None, out=out.append)
    assert code == 0
    assert "run 7 pending" in out[0] and "run 7 success" in out[-1]
    assert "build (ubuntu-latest): success" in out[-1]


def test_watch_reports_failure_and_timeout():
    assert watch("v0.6.1", fetch=_fake_ci([("completed", "failure")]),
                 sleep=lambda s: None, out=lambda s: None) == 1
    ticks = iter(range(0, 10_000, 100))
    assert watch("v0.6.1", fetch=_fake_ci([("in_progress", None)]), timeout=250,
                 sleep=lambda s: None, clock=lambda: next(ticks), out=lambda s: None) == 3


def test_watch_waits_for_run_to_appear():
    polls = {"n": 0}

    def fetch(url):
        if "/jobs" in url:
            return {"jobs": []}
        polls["n"] += 1
        runs = [] if polls["n"] < 3 else [
            {"id": 9, "head_branch": "v0.6.1", "event": "push", "status": "completed",
             "conclusion": "success"}]
        return {"workflow_runs": runs}

    out: list[str] = []
    assert watch("v0.6.1", fetch=fetch, sleep=lambda s: None, out=out.append) == 0
    assert "no CI run yet" in out[0]  # printed once, not every poll
    assert polls["n"] == 3


# -- published packages -------------------------------------------------------------------


def test_apt_and_cask_version_parsing():
    packages = "Package: other\nVersion: 1\n\nPackage: chattersyn\nVersion: 0.6.1\nArchitecture: amd64\n"
    assert apt_version(packages) == "0.6.1"
    assert apt_version("") is None
    assert cask_version('cask "chattersyn" do\n  version "0.6.1"\n  sha256 "x"\n') == "0.6.1"


def test_missing_assets_lists_absent_builds():
    release = {"assets": [{"name": "chattersyn-0.6.1-linux-x86_64.deb"},
                          {"name": "chattersyn-0.6.1-macos-arm64.zip"}]}
    assert missing_assets(release, "0.6.1") == [
        "chattersyn-0.6.1-linux-x86_64.tar.gz", "chattersyn-0.6.1-windows-x86_64.zip"]


def test_verify_all_published_and_stale_packages():
    full = {"assets": [{"name": f"chattersyn-0.6.1-{s}"} for s in (
        "linux-x86_64.deb", "linux-x86_64.tar.gz", "macos-arm64.zip", "windows-x86_64.zip")]}

    def json_ok(url):
        assert url == f"{REPO_API}/releases/tags/v0.6.1"
        return full

    def text(version):
        return lambda url: (f"Package: chattersyn\nVersion: {version}\n" if "Packages" in url
                            else f'  version "{version}"\n')

    assert verify("0.6.1", fetch_json=json_ok, fetch_text=text("0.6.1")) == []
    problems = verify("0.6.1", fetch_json=json_ok, fetch_text=text("0.6.0"))
    assert problems == ["APT index is at 0.6.0, expected 0.6.1",
                        "Homebrew cask is at 0.6.0, expected 0.6.1"]
