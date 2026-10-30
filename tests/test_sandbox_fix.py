"""The dev-only venv heal runs before image decodes only after a dev launcher
(scripts/_venv_fix.py) opted in — never in installed or frozen builds."""

from chattersyn.core import sandbox_fix


def test_heal_before_decode_needs_the_dev_opt_in(monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox_fix, "unhide_venv_files", lambda: calls.append("heal"))
    monkeypatch.setattr(sandbox_fix, "_lazy_heal", False)
    sandbox_fix.heal_before_decode()
    assert calls == []

    sandbox_fix.enable_lazy_heal()
    sandbox_fix.heal_before_decode()
    assert calls == ["heal"]
