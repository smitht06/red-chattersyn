# twitch-mod-commands Specification

## Purpose

Defines how ChatterSyn runs Twitch moderation and broadcaster chat commands through
Twitch's Helix API, which permissions they need, what the user sees when they work
or fail, and the mods/VIPs list and the user card's "Delete message" button.

Terms:

- The **channel** is the Twitch channel of the tab the command is typed in.
- A command's **permission** is the Twitch scope its Helix call needs:
  - moderator:manage:warnings: /warn
  - moderator:manage:announcements: /announce, /announceblue, /announcegreen,
    /announceorange, /announcepurple
  - moderator:manage:chat_settings: /slow, /slowoff, /followers, /followersoff,
    /emoteonly, /emoteonlyoff, /subscribers, /subscribersoff, /uniquechat,
    /uniquechatoff, /r9kbeta, /r9kbetaoff
  - moderator:manage:shoutouts: /shoutout
  - moderator:manage:shield_mode: /shield, /shieldoff
  - moderator:manage:chat_messages: /clear, /delete
  - channel:manage:moderators: /mod, /unmod, /mods
  - channel:manage:vips: /vip, /unvip, /vips
  - channel:manage:raids: /raid, /unraid
  - channel:edit:commercial: /commercial
  - channel:manage:broadcast: /marker, /settitle, /setgame
  - /uptime needs none.
- A **confirmation row** is one system row in the tab saying what happened:
  /warn "You warned <user>."; /mod "You added <user> as a moderator."; /unmod
  "You removed <user> as a moderator."; /vip "You added <user> as a VIP."; /unvip
  "You removed <user> as a VIP."; /slow "Slow mode on (<n> s)."; /slowoff "Slow mode
  off."; /followers "Followers-only mode on (<n> min)." ("(any follower)" for 0);
  /followersoff "Followers-only mode off."; /emoteonly, /subscribers, /uniquechat
  (and /r9kbeta) "<Emote-only | Subscribers-only | Unique-chat> mode on." and their
  -off forms "… mode off."; /shield "Shield mode on."; /shieldoff "Shield mode
  off."; /raid "Raiding <channel>."; /unraid "Raid cancelled."; /shoutout "You
  gave <channel> a shoutout."; /commercial "Commercial started (<n> s)."; /marker
  "Stream marker added."; /settitle "Title set to <title>."; /setgame "Category set
  to <category>.".

## Requirements

### Requirement: Commands run through Helix
Each command listed under Terms SHALL make its Helix call for the channel instead of
sending a chat line, with the arguments it accepts today and Twitch's former
defaults: /slow 30 seconds, /followers 0 minutes, /commercial 30 seconds.  No command listed under Terms
SHALL be sent as a chat message.

#### Scenario: THM-1 slow mode
- **WHEN** a mod types "/slow 10" in channel alice
- **THEN** ChatterSyn sets alice's chat to slow mode with a 10-second wait, and no
  chat message is sent

#### Scenario: THM-2 defaults
- **WHEN** the broadcaster types "/commercial" with no length
- **THEN** a 30-second commercial is started

#### Scenario: THM-3 announcement colour
- **WHEN** a mod types "/announceblue Stream starts soon"
- **THEN** an announcement "Stream starts soon" in blue is posted to the channel

#### Scenario: THM-4 user argument
- **WHEN** the broadcaster types "/vip @bob"
- **THEN** bob is made a VIP of the channel

#### Scenario: THM-5 custom action
- **WHEN** a user-card custom action runs "/shoutout {user.login}" on bob's card
- **THEN** a shoutout for bob is posted, exactly as if it had been typed

### Requirement: Command arguments and unchanged commands
Argument checks and usage rows SHALL stay as today, plus "Usage: /settitle <title>",
"Usage: /setgame <category>" and "Usage: /delete <message-id>". The other "not
implemented yet" commands, /help and /disconnect SHALL stay as they are.

#### Scenario: THM-26 settitle without text
- **WHEN** the broadcaster types "/settitle"
- **THEN** nothing is sent and the row says "Usage: /settitle <title>"

#### Scenario: THM-27 other stubs unchanged
- **WHEN** someone types "/poll"
- **THEN** the row still says "The /poll command is not implemented yet."

### Requirement: Missing permission asks to reconnect
When the signed-in login lacks a command's permission, ChatterSyn SHALL send nothing
and add the row "Reconnect your Twitch account (Connect Twitch Account…) to grant
ChatterSyn permission for /<command>.". Twitch sign-in SHALL request every
permission listed under Terms along with the existing ones.

#### Scenario: THM-6 old login
- **WHEN** a mod whose login predates this change types "/slow"
- **THEN** no request is sent and the row says "Reconnect your Twitch account
  (Connect Twitch Account…) to grant ChatterSyn permission for /slow."

#### Scenario: THM-7 sign-in asks for the new permissions
- **WHEN** the user starts "Connect Twitch"
- **THEN** the sign-in request includes all eleven permissions listed under Terms

#### Scenario: THM-8 not signed in
- **WHEN** a command listed under Terms is typed while no Twitch account is connected
- **THEN** no request is sent and the row says "Connect a Twitch account to
  moderate."

### Requirement: Success and failure feedback
After a command succeeds, ChatterSyn SHALL add a confirmation row, except for
announcements, /clear and /delete, whose result Twitch's chat already shows. When
Twitch refuses or fails a command (for example a target who is already a VIP, or
a message that is already gone), ChatterSyn SHALL add the row
"Twitch /<command> failed: <Twitch's reason>".

#### Scenario: THM-9 confirmation
- **WHEN** "/vip bob" succeeds
- **THEN** the tab shows the row "You added bob as a VIP."

#### Scenario: THM-10 no duplicate for announcements
- **WHEN** "/announce hello" succeeds
- **THEN** no confirmation row is added (the announcement appears in chat)

#### Scenario: THM-23 no row for /clear
- **WHEN** "/clear" succeeds
- **THEN** no confirmation row is added (Twitch clears the chat)

#### Scenario: THM-11 Twitch refuses
- **WHEN** a mod who is not the broadcaster types "/raid carol" and Twitch refuses
  it
- **THEN** the tab shows "Twitch /raid failed: " followed by Twitch's reason

### Requirement: Channel info commands
/settitle <text> SHALL set the channel's stream title. /setgame <name> SHALL set the
category whose name equals <name> ignoring case, or else Twitch's top search result
for <name>, and its confirmation row SHALL name the category set. /uptime SHALL
answer "<channel> has been live for <duration>" ("2h 14m", or "14m" under an
hour) or "<channel> is offline". /uptime needs no permission.

#### Scenario: THM-12 setgame best match
- **WHEN** the broadcaster types "/setgame gta 5" and no category is named "gta 5"
  but Twitch's top search result is "Grand Theft Auto V"
- **THEN** the category is set to Grand Theft Auto V and the row says "Category set
  to Grand Theft Auto V."

#### Scenario: THM-13 setgame exact match
- **WHEN** the broadcaster types "/setgame just chatting"
- **THEN** the category "Just Chatting" is set

#### Scenario: THM-14 uptime
- **WHEN** someone types "/uptime" in channel alice while alice has been live for
  2 hours 14 minutes
- **THEN** the row says "alice has been live for 2h 14m"

#### Scenario: THM-15 setgame without a match
- **WHEN** Twitch's search returns no category for <name>
- **THEN** nothing is changed and the row says "No Twitch category matches
  <name>."

### Requirement: Mods and VIPs list
/mods and /vips SHALL open a list popup titled "Moderators (<count>)" or
"VIPs (<count>)" with all of the channel's names (every page Twitch returns) sorted
A–Z and a box that filters them by name; an empty list shows "No moderators." or
"No VIPs.". Clicking a name SHALL open that user's card; Esc SHALL close the popup.
Twitch answers these lists only for the broadcaster; anyone else gets the failure
row.

#### Scenario: THM-16 list opens
- **WHEN** the broadcaster of alice types "/mods" and alice has the mods carol and
  bob
- **THEN** a popup titled "Moderators (2)" lists bob, then carol

#### Scenario: THM-17 filter
- **WHEN** the user types "ca" into the popup's filter box
- **THEN** only carol is listed

#### Scenario: THM-18 click opens the card
- **WHEN** the user clicks bob in the popup
- **THEN** bob's user card opens

#### Scenario: THM-24 a mod asks for the list
- **WHEN** a mod who is not the broadcaster types "/vips" and Twitch refuses
- **THEN** no popup opens and the tab shows "Twitch /vips failed: " followed by
  Twitch's reason

### Requirement: Delete message from the user card
A Twitch user card opened from one of that user's own chat messages SHALL show a
"Delete message" button when the user has the mod rights that show the card's Ban
and Timeout buttons. Pressing it
SHALL delete that message at once, without asking. Cards opened from an @mention or
/usercard SHALL NOT show the button. /delete <message-id> SHALL delete the message
with that id. A delete Twitch refuses SHALL show the failure row.

#### Scenario: THM-19 button deletes the message
- **WHEN** a mod opens bob's card from bob's message and presses "Delete message"
- **THEN** that message is deleted

#### Scenario: THM-20 no button without a message
- **WHEN** a mod opens bob's card through "/usercard bob"
- **THEN** the card has no "Delete message" button

#### Scenario: THM-21 no button without mod rights
- **WHEN** a user without mod rights opens bob's card from bob's message
- **THEN** the card has no "Delete message" button

### Requirement: Retired hosting commands
/host and /unhost SHALL send nothing and add the row "Twitch retired hosting — use
/raid <channel> instead." (with the literal text "<channel>"); they need no
permission.

#### Scenario: THM-22 host
- **WHEN** someone types "/host carol"
- **THEN** nothing is sent and the row says "Twitch retired hosting — use /raid
  <channel> instead."

#### Scenario: THM-25 unhost
- **WHEN** someone types "/unhost"
- **THEN** nothing is sent and the row says "Twitch retired hosting — use /raid
  <channel> instead."
