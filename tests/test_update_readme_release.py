"""README "Latest release" block: replaced in place, only the newest kept."""

import pytest
from update_readme_release import END, START, render_block, update

README = "# ChatterSyn\n\nIntro.\n\n## Development\n\nuv sync\n"


def test_appends_block_at_end_when_missing():
    out = update(README, render_block("v0.6.0", "2026-10-26", "- **Fixed** bans\n"))
    assert out.startswith(README.rstrip("\n"))
    assert out.rstrip().endswith(END)
    assert "## Latest release: v0.6.0 (2026-10-26)" in out
    assert "- **Fixed** bans" in out


def test_replaces_previous_release_so_only_latest_remains():
    first = update(README, render_block("v0.6.0", "2026-10-26", "- old notes"))
    second = update(first, render_block("v0.7.0", "2026-11-02", "- new notes"))
    assert second.count(START) == 1 and second.count(END) == 1
    assert "v0.7.0" in second and "- new notes" in second
    assert "v0.6.0" not in second and "- old notes" not in second
    assert second.startswith(README.rstrip("\n"))  # rest of the README untouched


def test_rerun_is_idempotent():
    block = render_block("v0.6.0", "2026-10-26", "- notes")
    once = update(README, block)
    assert update(once, block) == once


@pytest.mark.parametrize("version,date,notes,match", [
    ("0.6.0", "2026-10-26", "- x", "invalid version"),
    ("v0.6.0", "26/10/2026", "- x", "invalid date"),
    ("v0.6.0", "2026-10-26", "  \n", "empty"),
    ("v0.6.0", "2026-10-26", f"- x {START}", "markers"),
])
def test_render_rejects_bad_input(version, date, notes, match):
    with pytest.raises(ValueError, match=match):
        render_block(version, date, notes)


def test_broken_marker_pair_rejected():
    with pytest.raises(ValueError, match="broken"):
        update(README + START + "\n", render_block("v0.6.0", "2026-10-26", "- x"))
