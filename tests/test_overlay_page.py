"""OBS-15/16/17/18/19/20/21/23/24/26: the overlay page itself (static checks;
behaviour is exercised in a browser during the manual check)."""

import re
from pathlib import Path

from chattersyn.services.overlay_server import PAGE_PATH, load_page

ROOT = Path(__file__).resolve().parent.parent


def _page() -> str:
    return load_page().decode("utf-8")


def test_page_is_the_shipped_asset():
    assert PAGE_PATH.name == "chat.html" and PAGE_PATH.exists()
    assert "<title>" in _page()


def test_page_listens_to_events():
    """OBS-26: EventSource reconnects by itself and keeps the rows."""
    page = _page()
    assert "new EventSource(\"/events\")" in page
    for kind in ('"config"', '"message"', '"remove"'):
        assert f"addEventListener({kind}" in page


def test_page_inserts_text_with_textcontent_only():
    """Review Focus 4 / OBS-21: chat content can never become markup or code."""
    page = _page()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                   "eval(", "new Function"):
        assert banned not in page, banned
    assert ".textContent" in page


def test_platform_icons_inline():
    page = _page()
    assert "#9146ff" in page.lower() and "#ff0000" in page.lower()


def test_defaults_match_settings():
    """OBS-15: the page's own fallback look is the default look."""
    page = _page()
    assert "--size: 20px" in page
    assert "--text: #ffffff" in page
    assert "--bg: transparent" in page


def test_appearance_switches_present():
    """OBS-16..20, 23, 24: every setting the page reads."""
    page = _page()
    for key in ("font_family", "font_size", "text_color", "background",
                "background_fills_source", "outline",
                "show_platform_icons", "show_badges", "show_emotes", "show_timestamps",
                "fade_seconds", "max_messages"):
        assert re.search(rf"\b{key}\b", page), key


def test_pyinstaller_bundles_the_page():
    spec = (ROOT / "chattersyn.spec").read_text(encoding="utf-8")
    assert '"overlay" / "chat.html"' in spec


def test_background_fills_the_whole_source():
    """User request: the background colour is for the whole window, on a layer
    OBS's default CSS (body { background-color: rgba(0,0,0,0) }) can't override."""
    page = _page()
    assert '<div id="bg"></div>' in page
    assert re.search(r"#bg \{[^}]*inset: 0;[^}]*background: var\(--bg\)", page)
    row_rule = re.search(r"\.row \{[^}]*\}", page).group(0)
    assert "background" not in row_rule


def test_background_can_sit_behind_messages_only():
    """User request: a switch between the whole source and just the messages."""
    page = _page()
    assert re.search(r"body\.bg-rows #bg \{[^}]*display: none", page)
    assert re.search(r"body\.bg-rows \.row \{[^}]*background: var\(--bg\)", page)
    assert 'classList.toggle("bg-rows", config.background_fills_source === false)' in page


def test_fading_rule_comes_last():
    """Final review #11: .row.fading must win over .row.event's opacity."""
    page = _page()
    assert page.index(".row.fading") > page.index(".row.event")
