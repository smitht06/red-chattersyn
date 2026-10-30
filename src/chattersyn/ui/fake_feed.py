"""FakeFeed: M1 development feed emitting Twitch/YouTube-style messages.

Replaced by the real Twitch IRC / YouTube services in M2/M3; kept behind the
`--demo` flag for UI work without credentials.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import uuid

from ..models.emote import Emote
from ..models.message import Author, Badge, ImageRun, Message, MessageFlags, Platform, TextRun
from ..services.twitch.mapper import twitch_emote_urls

NAMES = ["alice", "bob_the_builder", "charlieTV", "dana_kappa", "eveLUL", "frank_", "gwen7",
         "hank_watchin", "iris_uwu", "james_pog", "katePlays", "leo_streams"]
COLORS = ["#ff0000", "#1e90ff", "#2ba640", "#b22222", "#ff8c1a", "#a970ff", "#5f9ea0",
          "#ff69b4", "#8a2be2", "#d2691e", "#3cb371", "#c71585"]
# Twitch global emotes under their real, public and stable IDs, so --demo
# shows emote art (and store screenshots do too) instead of placeholder boxes.
TWITCH_EMOTES = [
    Emote(emote_id, name, "twitch", urls=twitch_emote_urls(emote_id), height_px=28)
    for emote_id, name in (("25", "Kappa"), ("305954156", "PogChamp"),
                           ("425618", "LUL"), ("354", "4Head"))
]

# A real animated 7TV emote (PETPET) so --demo visibly plays a moving emote
# instead of only showing placeholder boxes. The CDN URL is public and stable.
_ANIMATED_EMOTE = Emote(
    "01FSNK0GP000038GDBZ91HT6SQ", "PETPET", "7tv",
    urls={
        "1x": "https://cdn.7tv.app/emote/01FSNK0GP000038GDBZ91HT6SQ/1x.webp",
        "2x": "https://cdn.7tv.app/emote/01FSNK0GP000038GDBZ91HT6SQ/2x.webp",
    },
    animated=True,
    height_px=32,
)
YOUTUBE_LINES = [
    "hello everyone!! 😄",
    "this stream is great 🎉",
    "who else is here from the raid?",
    "LET'S GOOO 🚀🚀",
    "chat is this real 💀",
    "day 47 of asking for a shoutout",
    "w stream w chat",
    "greetings from Germany 🇩🇪",
    "first time here, loving it",
    "can we hit 100 likes??",
]
TWITCH_LINES = [
    "this game is actually insane",
    "KEKW chat knows",
    "did you see that play",
    "monkaS that was close",
    "PogChamp what a run",
    "streamer is cracked today",
    "lore check??",
    "EZ Clap",
    "whats the song called",
    "LUL he walked right into it",
]
def _twitch_badge_urls(guid: str) -> dict[str, str]:
    # Twitch's public global-badge art (the same URLs Helix hands out).
    base = f"https://static-cdn.jtvnw.net/badges/v1/{guid}"
    return {"1x": f"{base}/1", "2x": f"{base}/2"}


BADGE_POOL = [
    Badge(set_id="broadcaster", version_id="1", title="Broadcaster",
          urls=_twitch_badge_urls("5527c58c-fb7d-422d-b71b-f309dcb85cc1")),
    Badge(set_id="moderator", version_id="1", title="Moderator",
          urls=_twitch_badge_urls("3267646d-33f0-4b17-b3df-f923a41db1d0")),
    Badge(set_id="vip", version_id="1", title="VIP",
          urls=_twitch_badge_urls("b817aba4-fad8-49e2-b88a-7cc744dfa6ec")),
    Badge(set_id="subscriber", version_id="0", title="Subscriber",
          urls=_twitch_badge_urls("5d9f2208-5dd8-11e7-8513-2ff4adfae661")),
]


class FakeFeed:
    def __init__(self, tab, twitch_channel: str = "redcyberdragon29", youtube_title: str = "Demo Stream"):
        self.tab = tab
        self.twitch_channel = twitch_channel
        self.youtube_title = youtube_title
        self._task: asyncio.Task | None = None
        self._running = False
        self._authors: dict[str, Author] = {}

    def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _author(self, name: str, platform: Platform) -> Author:
        key = f"{platform.value}:{name}"
        if key not in self._authors:
            color = random.choice(COLORS) if platform is Platform.TWITCH else None
            badges = [random.choice(BADGE_POOL)] if random.random() < 0.25 and platform is Platform.TWITCH else []
            self._authors[key] = Author(name=name, login=name, user_id=name, color=color, badges=badges)
        return self._authors[key]

    def _twitch_message(self) -> Message:
        name = random.choice(NAMES)
        text = random.choice(TWITCH_LINES)
        runs: list = []
        if "KEKW" in text:
            runs.append(ImageRun(_ANIMATED_EMOTE, "KEKW"))
        else:
            runs.append(TextRun(text))
        if random.random() < 0.4:
            emote = random.choice(TWITCH_EMOTES)
            runs.append(ImageRun(emote, emote.name))
        plain = text
        return Message(
            id=uuid.uuid4().hex,
            platform=Platform.TWITCH,
            channel_id=self.twitch_channel,
            ts=dt.datetime.now(),
            author=self._author(name, Platform.TWITCH),
            runs=runs,
            plain=plain,
            flags=random.choice([MessageFlags.NONE, MessageFlags.NONE, MessageFlags.FIRST_MESSAGE]),
        )

    def _youtube_message(self) -> Message:
        name = random.choice(NAMES)
        text = random.choice(YOUTUBE_LINES)
        return Message(
            id=uuid.uuid4().hex,
            platform=Platform.YOUTUBE,
            channel_id="yt-demo",
            ts=dt.datetime.now(),
            author=self._author(name, Platform.YOUTUBE),
            runs=[TextRun(text)],
            plain=text,
            flags=random.choice([MessageFlags.NONE, MessageFlags.SUPERCHAT] if random.random() < 0.06 else [MessageFlags.NONE]),
            extra={"amount": "€2.00"} if random.random() < 0.06 else {},
        )

    def _system_message(self) -> Message:
        text = random.choice([
            f"{random.choice(NAMES)} subscribed at Tier 1!",
            f"{random.choice(NAMES)} gifted 5 subs to the community!",
            f"{random.choice(NAMES)} is new here. Say hello!",
        ])
        return Message.system(Platform.TWITCH if random.random() < 0.5 else Platform.YOUTUBE,
                              self.twitch_channel, text)

    async def _run(self) -> None:
        while self._running:
            await asyncio.sleep(random.uniform(0.08, 0.3))
            roll = random.random()
            if roll < 0.04:
                msg = self._system_message()
            elif roll < 0.5:
                msg = self._twitch_message()
            else:
                msg = self._youtube_message()
            self.tab.add_message(msg)

    def handle_send(self, text: str) -> None:
        """M1: local echo into both platform feeds."""
        you_tw = Author(name="you", login="you", user_id="you", color="#7a5cff",
                        badges=[Badge("vip", "1", "VIP")])
        you_yt = Author(name="you", login="you", user_id="you")
        self.tab.add_message(Message.text(Platform.TWITCH, self.twitch_channel, you_tw, text))
        self.tab.add_message(Message.text(Platform.YOUTUBE, "yt-demo", you_yt, text))
