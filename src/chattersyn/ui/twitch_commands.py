"""Twitch moderation and broadcaster commands run through Helix, for the main
window: permission check, the Helix call, and the confirmation row, name list or
failure row. Moved out of main_window.py; MainWindow keeps thin delegates."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import TYPE_CHECKING

from ..services.twitch.mod_commands import COMMAND_SCOPES, HelixCommand, run_helix_command
from .user_list_popup import UserListPopup

if TYPE_CHECKING:
    from .tab import Tab


async def twitch_helix_command(win, tab: Tab, cmd: HelixCommand) -> None:
    """Run one moderation/broadcaster command through Helix and show the
    confirmation, the name list, or why it failed."""
    twitch = win._twitch
    helix = twitch.helix if twitch is not None else None
    tokens = twitch.tokens if twitch is not None else None
    if helix is None or tokens is None:
        win._twitch_note(tab, "Connect a Twitch account to moderate.")
        return
    scope = COMMAND_SCOPES.get(cmd.name)
    if scope is not None and scope not in tokens.scopes:
        win._twitch_note(
            tab, "Reconnect your Twitch account (Connect Twitch Account…) "
                 f"to grant ChatterSyn permission for /{cmd.name}.")
        return
    try:
        channel = await helix.user_by_login(tab.twitch_channel)
        if not channel or not channel.get("id"):
            win._twitch_note(tab, f"No Twitch channel named {tab.twitch_channel}.")
            return
        result = await run_helix_command(
            helix, cmd, channel=tab.twitch_channel, broadcaster_id=channel["id"],
            moderator_id=tokens.user_id, now=dt.datetime.now(dt.UTC))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = (str(exc).splitlines() or [""])[0].strip() or type(exc).__name__
        win._twitch_note(tab, f"Twitch /{cmd.name} failed: {reason}")
        return
    if result.names is not None:
        win._show_user_list(tab, cmd.name, result.names)
    elif result.row:
        win._twitch_note(tab, result.row)


def show_user_list(win, tab: Tab, name: str, names: list[str]) -> None:
    """/mods and /vips result: the filterable list popup at the cursor."""
    if name == "mods":
        title, empty = f"Moderators ({len(names)})", "No moderators."
    else:
        title, empty = f"VIPs ({len(names)})", "No VIPs."
    if win._user_list_popup is not None:
        win._user_list_popup.close()
    popup = UserListPopup(title, names, empty, parent=win)
    popup.user_clicked.connect(lambda login: win._open_usercard_by_login(tab, login))
    popup.closed.connect(win._on_user_list_closed)
    win._user_list_popup = popup
    popup.show_at_cursor()
