# twitch-own-messages Specification

## Purpose

Gives the chat rows of messages the user sends on Twitch the real message id Twitch
assigns, so deletes, timeouts and replies that target those messages find them.

Terms: a **waiting send** is a message the user sent in a channel whose id has not
arrived yet; a **send refusal** is a Twitch NOTICE whose msg-id starts with "msg_"
(for example msg_duplicate, msg_ratelimit, msg_slowmode).

## Requirements

### Requirement: Sent messages take Twitch's id
When Twitch answers in a channel with a USERSTATE that carries an `id` tag, the
oldest waiting send in that channel SHALL take that id. A send refusal in a channel
SHALL remove the oldest waiting send there without giving it an id. A USERSTATE
without an `id` tag SHALL change no waiting send. Waiting sends in other channels
SHALL NOT be affected. A waiting send that gets no answer within 60 seconds SHALL
stop waiting and keep its placeholder id.

#### Scenario: OMI-1 id arrives
- **WHEN** the user sends "hello" in channel alice and Twitch answers with a
  USERSTATE for alice carrying id "abc-1"
- **THEN** the row for "hello" has the id "abc-1"

#### Scenario: OMI-2 two sends in order
- **WHEN** the user sends "one" then "two" in alice, and USERSTATEs with ids "id-1"
  then "id-2" arrive
- **THEN** "one" has id "id-1" and "two" has id "id-2"

#### Scenario: OMI-3 refused send
- **WHEN** the user sends "one" then "two" in alice, Twitch refuses "one" with a
  msg_duplicate NOTICE, then answers a USERSTATE with id "id-2"
- **THEN** "two" has id "id-2" and "one" keeps its placeholder id

#### Scenario: OMI-4 join USERSTATE
- **WHEN** a USERSTATE without an id arrives (as on joining) while "hello" is waiting
- **THEN** "hello" is still waiting and takes the id of the next USERSTATE with one

#### Scenario: OMI-5 channels kept apart
- **WHEN** "hi" waits in alice and a USERSTATE with id "b-1" arrives for bob
- **THEN** "hi" is still waiting

#### Scenario: OMI-6 deleted by a mod
- **WHEN** the user's "hello" got id "abc-1" and Twitch then deletes message
  "abc-1" (CLEARMSG)
- **THEN** the "hello" row is shown as deleted
