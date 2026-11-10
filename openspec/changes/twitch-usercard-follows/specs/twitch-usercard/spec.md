## Purpose

Defines what a Twitch user card shows when the user is signed in to Twitch, where
each detail comes from, and which details need which Twitch permission.

Terms: the **card's channel** is the Twitch channel of the tab the card was opened
from; **mod rights** means the signed-in user is the card's channel's broadcaster
or a moderator there; the **follow permission** is the Twitch scope
`moderator:read:followers`.

## ADDED Requirements

### Requirement: Card details load independently
When the user is signed in to Twitch, a Twitch user card SHALL show the user's
avatar, account creation date and follower count, each as soon as its lookup
answers. A lookup that fails SHALL hide only its own row and SHALL NOT hide the
other details.

#### Scenario: TUF-1 signed-in card fills in
- **WHEN** a signed-in user opens the card of a chatter whose lookups all answer
- **THEN** the card shows the chatter's avatar, creation date and follower count

#### Scenario: TUF-2 follow lookup fails
- **WHEN** the follow lookup fails and the other lookups answer
- **THEN** the card still shows the avatar, creation date and follower count, and
  no follow row

#### Scenario: TUF-3 follower count fails
- **WHEN** the follower-count lookup fails
- **THEN** the card still shows the avatar and creation date, and no follower row

### Requirement: Follow date for the card's channel
When the signed-in user has mod rights in the card's channel and the token holds
the follow permission, the card SHALL show "Followed <channel> since: <date>" with
the date the card's user started following the card's channel. When the card's
user does not follow the channel, the card SHALL show no follow row. Without mod
rights, without the follow permission, or on the card of the channel's own
broadcaster, ChatterSyn SHALL NOT ask Twitch and SHALL show no follow row.

#### Scenario: TUF-4 mod sees the follow date
- **WHEN** a mod of channel "alice" opens the card of chatter "bob", who followed
  alice on 2024-03-05
- **THEN** the card shows "Followed alice since: 2024-03-05"

#### Scenario: TUF-5 chatter does not follow
- **WHEN** a mod opens the card of a chatter who does not follow the card's channel
- **THEN** the card shows no follow row

#### Scenario: TUF-6 no mod rights
- **WHEN** a signed-in user without mod rights in the card's channel opens a card
- **THEN** no follow lookup is sent and the card shows no follow row

#### Scenario: TUF-7 token without the follow permission
- **WHEN** a mod whose token lacks the follow permission opens a card
- **THEN** no follow lookup is sent and the card shows no follow row

#### Scenario: TUF-8 broadcaster's own card
- **WHEN** a card is opened for the card's channel's own broadcaster
- **THEN** no follow lookup is sent and the card shows no follow row

### Requirement: Sign-in asks for the follow permission
Twitch sign-in SHALL request the follow permission together with the existing
permissions. ChatterSyn SHALL NOT prompt users whose saved login lacks it; their
cards show no follow row until they reconnect Twitch.

#### Scenario: TUF-9 new sign-in requests the permission
- **WHEN** the user starts "Connect Twitch"
- **THEN** the sign-in request asks for `moderator:read:followers` along with
  chat:read, chat:edit and moderator:manage:banned_users
