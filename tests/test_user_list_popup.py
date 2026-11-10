"""THM-16, THM-17, THM-18: the mods/VIPs list popup."""

from chattersyn.ui.user_list_popup import UserListPopup


def _shown(popup):
    return popup.visible_names()


def test_title_and_sorted_names(qapp):
    """THM-16: titled, names sorted case-insensitively."""
    popup = UserListPopup("Moderators (2)", ["carol", "Bob"], "No moderators.")
    assert popup.title_text() == "Moderators (2)"
    assert _shown(popup) == ["Bob", "carol"]


def test_filter_box(qapp):
    """THM-17: case-insensitive substring filter."""
    popup = UserListPopup("Moderators (2)", ["bob", "carol"], "No moderators.")
    popup.filter_box.setText("CA")
    assert _shown(popup) == ["carol"]
    popup.filter_box.setText("")
    assert _shown(popup) == ["bob", "carol"]


def test_click_emits_user(qapp):
    """THM-18: clicking a name emits it."""
    popup = UserListPopup("VIPs (2)", ["bob", "carol"], "No VIPs.")
    got = []
    popup.user_clicked.connect(got.append)
    popup.list_widget.itemClicked.emit(popup.list_widget.item(0))
    assert got == ["bob"]


def test_empty_list_text(qapp):
    popup = UserListPopup("VIPs (0)", [], "No VIPs.")
    assert _shown(popup) == []
    assert popup.empty_label.text() == "No VIPs."
    assert not popup.empty_label.isHidden()
    full = UserListPopup("VIPs (1)", ["bob"], "No VIPs.")
    assert full.empty_label.isHidden()
