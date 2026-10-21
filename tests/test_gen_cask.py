"""Cask-generation tests (template substitution, no brew required)."""

from pathlib import Path

import pytest
from gen_cask import generate

SHA = "0" * 64


def test_generate_substitutes_and_copies_readme(tmp_path: Path):
    out = tmp_path / "tap"
    generate("0.4.1", SHA, out)

    cask = (out / "Casks" / "chattersyn.rb").read_text()
    assert 'version "0.4.1"' in cask
    assert f'sha256 "{SHA}"' in cask
    assert "__" not in cask  # no placeholders left
    # brew interpolates #{version} into the url at install time, so the
    # literal stays in the cask and the version lands only in the stanza.
    assert 'chattersyn-#{version}-macos-arm64.zip' in cask

    readme = (out / "README.md").read_text()
    assert "brew tap smitht06/chattersyn" in readme


def test_generate_rejects_bad_sha256(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid sha256"):
        generate("0.4.1", "not-a-hash", tmp_path / "tap")


def test_generate_rejects_leftover_placeholders(tmp_path: Path, monkeypatch):
    tpl = tmp_path / "tpl"
    (tpl / "Casks").mkdir(parents=True)
    (tpl / "Casks" / "chattersyn.rb").write_text('cask "__OTHER__"')
    (tpl / "README.md").write_text("readme")
    monkeypatch.setattr("gen_cask.TEMPLATE_DIR", tpl)
    with pytest.raises(ValueError, match="placeholders remain"):
        generate("0.4.1", SHA, tmp_path / "tap")
