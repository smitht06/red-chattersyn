# twitch-helix-mod-commands — plan

## Goal

Make every Twitch moderation and broadcaster command ChatterSyn lists work through
Helix, with permission checks, confirmations, a mods/VIPs list popup and a
"Delete message" button on the user card.

## Architecture

`helix.py` gains one method per endpoint. A new `services/twitch/mod_commands.py`
holds the command → permission table and `run_helix_command`, which makes the
Helix call(s) for one parsed command and returns its confirmation text (or a name
list for /mods and /vips). `commands.py` parses the typed command into a
`HelixCommand` instead of a PRIVMSG; MainWindow resolves the channel and user ids,
checks the permission, runs it, and shows rows or opens the new `UserListPopup`.
The user card's "Delete message" button sends "/delete <id>" through the same path.

**Spec:** openspec/changes/twitch-helix-mod-commands/specs/twitch-mod-commands/spec.md

## Global Constraints

- Commit or push only when the user asks; never add a `Co-Authored-By` trailer (CLAUDE.md).
- Tests use no credentials and no network: respx for Helix, fakes for MainWindow.
- Exact texts as the spec: the confirmation rows listed under its Terms; "Reconnect your Twitch account (Connect Twitch Account…) to grant ChatterSyn permission for /<command>."; "Connect a Twitch account to moderate."; "Twitch /<command> failed: <reason>"; "Twitch retired hosting — use /raid <channel> instead."; "No Twitch category matches <name>."; "<channel> has been live for <duration>" / "<channel> is offline"; "Moderators (<n>)", "VIPs (<n>)", "No moderators.", "No VIPs."; usage rows "Usage: /settitle <title>", "Usage: /setgame <category>", "Usage: /delete <message-id>".
- Scope strings exactly: moderator:manage:warnings, moderator:manage:announcements, moderator:manage:chat_settings, moderator:manage:shoutouts, moderator:manage:shield_mode, moderator:manage:chat_messages, channel:manage:moderators, channel:manage:vips, channel:manage:raids, channel:edit:commercial, channel:manage:broadcast.
- Behaviour the spec does not name stays: /ban /timeout /unban paths, other stubs, /help, /disconnect, /me, YouTube commands.
- `uv run ruff check src/ tests/` and `uv run pytest -q` stay green after every task.

## Review Focus

1. `PATCH /chat/settings` must send only the field the command changes; sending the whole settings object would switch other modes off → task 1.1 pins `test_chat_settings_sends_only_given_fields`.
2. /mods and /vips must follow Twitch's `pagination.cursor` to the end → task 1.1 pins `test_list_moderators_follows_pages`.
3. Ids: broadcaster = the tab's channel, moderator = the token's own `user_id`, /raid's target and every user argument resolved by login → task 2.2 pins `test_raid_resolves_target_channel_id`.
4. The scope check reads the live `tokens.scopes` (synced from /validate), so an old login never sends → task 2.2 pins it in the THM-6 test.
5. The "Delete message" button needs a real Twitch message id: never on YouTube cards, synthetic rows or @mention cards → task 3.2 pins `test_no_delete_button_on_youtube_card`.

## 1. Twitch API

- [x] 1.1 Helix endpoints and the new scopes — verify: uv run pytest tests/test_helix.py tests/test_twitch_auth.py -q passes

Covers: THM-7 · Test: scenario-first

Files: modify src/chattersyn/services/twitch/helix.py, src/chattersyn/services/twitch/auth.py; test tests/test_helix.py, tests/test_twitch_auth.py

Interfaces:
- auth.py: constants for the 11 scopes (names `WARNINGS_SCOPE`, `ANNOUNCEMENTS_SCOPE`, `CHAT_SETTINGS_SCOPE`, `SHOUTOUTS_SCOPE`, `SHIELD_SCOPE`, `CHAT_MESSAGES_SCOPE`, `MODERATORS_SCOPE`, `VIPS_SCOPE`, `RAIDS_SCOPE`, `COMMERCIAL_SCOPE`, `BROADCAST_SCOPE`) appended to `SCOPES` after the existing four.
- helix.py, all raising `HelixError(<Twitch message>)` on failure via `_write` (add a `_send(method, path, params, body) -> dict` that returns the JSON body for calls whose answer is needed):
  `warn_user(b, m, user_id, reason)`, `add_moderator(b, user_id)`, `remove_moderator(b, user_id)`, `add_vip(b, user_id)`, `remove_vip(b, user_id)`, `list_moderators(b) -> list[str]` and `list_vips(b) -> list[str]` (logins, every page, `first=100`), `send_announcement(b, m, message, color)` (`color` "primary"/"blue"/"green"/"orange"/"purple"), `update_chat_settings(b, m, **fields)` (body = exactly the given fields), `send_shoutout(b, m, to_id)`, `set_shield_mode(b, m, active)`, `delete_chat_messages(b, m, message_id=None)`, `start_raid(b, to_id)`, `cancel_raid(b)`, `start_commercial(b, length)`, `create_marker(user_id)`, `modify_channel(b, *, title=None, game_id=None)`, `search_categories(query) -> list[dict]` (`{id, name}`, Twitch's order). `b` = broadcaster id, `m` = moderator id (the token user).

1. Write tests: one respx test per method asserting method, path, query and JSON body; `test_chat_settings_sends_only_given_fields` (`update_chat_settings(b, m, slow_mode=True, slow_mode_wait_time=10)` → body exactly those two keys); `test_list_moderators_follows_pages` (two pages via `pagination.cursor`); `test_helix_error_carries_twitch_message`; `test_sign_in_requests_mod_command_scopes` (authorize URL `scope` = the 4 existing + the 11 new, in order). Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `feat(twitch): Helix endpoints and scopes for moderation commands`.

- [x] 1.2 Command runner and confirmation texts — verify: uv run pytest tests/test_twitch_mod_commands.py -q passes

Covers: THM-1, THM-2, THM-3, THM-4, THM-9, THM-10, THM-12, THM-13, THM-14, THM-15, THM-23 · Test: scenario-first

Files: create src/chattersyn/services/twitch/mod_commands.py; test tests/test_twitch_mod_commands.py

Interfaces:
- Consumes: the helix methods and scope constants (1.1); `Helix.user_by_login`, `Helix.streams`.
- Produces: `@dataclass HelixCommand(name: str, user: str = "", text: str = "", seconds: int = 0, message_id: str = "")` (name is the typed command, lowercase, e.g. "announceblue", "r9kbeta").
- Produces: `COMMAND_SCOPES: dict[str, str | None]` (every command name in the spec's Terms → its scope; "uptime" → None).
- Produces: `@dataclass HelixResult(row: str = "", names: list[str] | None = None)` and `async run_helix_command(helix, cmd, *, channel: str, broadcaster_id: str, moderator_id: str, now: datetime) -> HelixResult` — resolves user/channel arguments by login (unknown → raises `HelixError("No Twitch user named <user>")`), makes the call(s), returns the confirmation row from the spec's Terms ("" for announcements, /clear, /delete), `names` for /mods and /vips (sorted A–Z, case-insensitive), the /uptime answer ("2h 14m", "14m" under an hour, from `started_at`), and /setgame's exact-else-top match ("No Twitch category matches <name>." when the search is empty, no PATCH sent).

1. Write tests (respx Helix): `test_slow_sets_wait_time` (THM-1: PATCH body `{"slow_mode": true, "slow_mode_wait_time": 10}`, row "Slow mode on (10 s)."), `test_commercial_default_30` (THM-2), `test_announce_blue` (THM-3: color "blue", row ""), `test_vip_resolves_user` (THM-4/THM-9: row "You added bob as a VIP."), `test_followers_zero_any_follower`, `test_announce_has_no_row` (THM-10), `test_clear_has_no_row` (THM-23), `test_setgame_top_search_hit` (THM-12), `test_setgame_exact_match_ignores_case` (THM-13), `test_setgame_no_match_changes_nothing` (THM-15), `test_uptime_live_and_offline` (THM-14 with an injected `now`), `test_every_command_has_a_scope_entry`, `test_mods_sorted_names`, `test_unknown_user_raises`. Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `feat(twitch): run moderation commands through Helix`.

## 2. Commands

- [x] 2.1 Parse commands into HelixCommand; hosting and usage rows — verify: uv run pytest tests/test_commands.py -q passes

Covers: THM-22, THM-25, THM-26, THM-27 · Test: scenario-first

Files: modify src/chattersyn/services/commands.py; test tests/test_commands.py

Interfaces:
- Consumes: `HelixCommand` (1.2).
- `CommandOutcome.twitch_helix: HelixCommand | None = None`. The 31 non-ban names of `_IRC_MOD_COMMANDS` plus delete, mods, vips, settitle, setgame, uptime return a `HelixCommand` (after today's argument checks; `@` stripped from user args; /slow and /commercial seconds with defaults 30; /followers a duration like /timeout's, bare number = minutes; `seconds` always holds seconds and the runner sends /followers as whole minutes). Those six leave `_MOD_STUB_COMMANDS`. "host" and "unhost" leave `_PASSTHROUGH_COMMANDS` and answer the hosting row. ban/timeout/unban keep `ModAction`.
- New usage rows: "Usage: /settitle <title>", "Usage: /setgame <category>", "Usage: /delete <message-id>".

1. Write tests: `test_slow_becomes_helix_command` (`/slow 10` → HelixCommand("slow", seconds=10), no send_text), `test_slow_default_30`, `test_followers_duration` (`/followers 1h` → seconds=3600; `/followers 10` → 600), `test_vip_strips_at`, `test_host_explains_raid` (THM-22) and `test_unhost_explains_raid` (THM-25), `test_settitle_needs_text` (THM-26), `test_poll_still_stub` (THM-27), `test_no_mod_command_is_sent_as_chat` (every listed name → `send_text == ""`). Update existing tests that expected PRIVMSG text for these commands. Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `feat(commands): moderation commands become Helix commands`.

- [x] 2.2 Run Helix commands from MainWindow — verify: uv run pytest tests/test_main_window_mod_commands.py -q passes

Covers: THM-5, THM-6, THM-8, THM-11 · Test: scenario-first

Files: modify src/chattersyn/ui/main_window.py; test tests/test_main_window_mod_commands.py (new; fake TwitchService with a respx-backed Helix, fixture style of tests/test_main_window_usercard_enrich.py)

Interfaces:
- Consumes: `CommandOutcome.twitch_helix` (2.1), `COMMAND_SCOPES`, `run_helix_command`, `HelixResult` (1.2).
- `async _twitch_helix_command(tab, cmd)`: no Helix/tokens → "Connect a Twitch account to moderate."; scope missing → the reconnect row naming `/<cmd.name>`; else resolve the tab channel's broadcaster id, call `run_helix_command(..., moderator_id=tokens.user_id, now=<aware UTC now>)`; a non-empty `row` → one system row; `names` → `_show_user_list(tab, cmd.name, names)` (stub until 3.1: a row); `HelixError`/other exceptions → "Twitch /<name> failed: <reason>".

1. Write tests: `test_custom_action_runs_helix_command` (THM-5: `_on_usercard_action(tab, target, "/shoutout bob")` → shoutout POST), `test_old_login_asks_to_reconnect` (THM-6: no request sent), `test_not_signed_in` (THM-8), `test_twitch_refusal_row` (THM-11: 400 with message → "Twitch /raid failed: <message>"), `test_raid_resolves_target_channel_id` (Review Focus 3), `test_confirmation_row_added`. Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `feat(ui): run Twitch moderation commands through Helix`.

## 3. Popups

- [ ] 3.1 Mods/VIPs list popup — verify: uv run pytest tests/test_user_list_popup.py tests/test_main_window_mod_commands.py -q passes

Covers: THM-16, THM-17, THM-18, THM-24 · Test: scenario-first

Files: create src/chattersyn/ui/user_list_popup.py; modify src/chattersyn/ui/main_window.py; test tests/test_user_list_popup.py, tests/test_main_window_mod_commands.py

Interfaces:
- Produces: `UserListPopup(BasePopup)(title: str, names: list[str], empty_text: str, parent=None)` with `user_clicked = Signal(str)`; a filter `QLineEdit` (case-insensitive substring), a list of names; Esc closes (BasePopup).
- MainWindow `_show_user_list(tab, name, names)`: title "Moderators (<n>)" / "VIPs (<n>)", empty text "No moderators." / "No VIPs.", `user_clicked` → `_open_usercard_by_login(tab, login)`; shown at the cursor like the user card.

1. Write tests: `test_title_and_sorted_names` (THM-16), `test_filter_box` (THM-17), `test_click_emits_user` and the MainWindow `test_click_opens_usercard` (THM-18), `test_empty_list_text`, `test_mod_refused_shows_row_no_popup` (THM-24). Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run pytest -q`. Expected: PASS.
4. Commit `feat(ui): mods and VIPs list popup`.

- [ ] 3.2 "Delete message" on the user card — verify: uv run pytest tests/test_usercard.py tests/test_main_window_mod_commands.py -q passes

Covers: THM-19, THM-20, THM-21 · Test: scenario-first

Files: modify src/chattersyn/ui/usercard.py, src/chattersyn/ui/main_window.py; test tests/test_usercard.py, tests/test_main_window_mod_commands.py

Interfaces:
- `UsercardPopup(..., delete_message_id: str = "")`: when non-empty (and the mod frame is shown) a "Delete message" button emits `action_requested("/delete <id>")`.
- MainWindow `_show_usercard` passes `delete_message_id=msg.id` only for a Twitch card opened from the target's own, non-synthetic, non-system message (the existing `own_message` rule of `_usercard_custom_actions`) when `self_is_mod`.

1. Write tests: `test_delete_button_sends_delete` (THM-19: pressing it → `action_requested("/delete m-1")`, and through MainWindow a `DELETE /moderation/chat?message_id=m-1`), `test_no_delete_button_from_usercard_command` (THM-20), `test_no_delete_button_without_mod_rights` (THM-21), `test_no_delete_button_on_youtube_card` (Review Focus 5). Run. Expected: FAIL.
2. Implement.
3. Run the verify command, then `uv run ruff check src/ tests/` and `uv run pytest -q`. Expected: PASS.
4. Commit `feat(ui): delete a message from the Twitch user card`.
