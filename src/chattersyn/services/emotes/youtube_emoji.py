"""Emotes on the YouTube side of a dual chat.

YouTube live chat only accepts plain text (liveChatMessages.insert carries a
single messageText), so a Twitch/7TV/BTTV/FFZ emote can never reach
youtube.com as an image. So:

- `youtube_emote_text` rewrites the YouTube copy of an outgoing message:
  emote names are removed (emotes go to Twitch only), or — opt-in
  ``youtube_emotes.emoji`` — swapped for an emoji from an editable table
  (``youtube_emotes.map``; DEFAULT_EMOJI until the user edits it). A message
  with nothing left (`has_youtube_text` False) isn't sent to YouTube at all.
- `emote_runs` turns known emote names in YouTube text into image runs, so
  ChatterSyn itself renders them on YouTube rows (visible in ChatterSyn only).
"""

from __future__ import annotations

import re

from ...core.text import PUNCT_STRIP
from ...models.message import ImageRun, TextRun
from .emote_registry import EmoteRegistry

# Common global Twitch/BTTV/7TV names. Case-sensitive, matched as whole words.
DEFAULT_EMOJI: dict[str, str] = {
    "Kappa": "😏", "LUL": "😂", "KEKW": "🤣", "OMEGALUL": "🤣", "PogChamp": "😮",
    "Pog": "😮", "PogU": "😮", "POGGERS": "😮", "PepeHands": "😢", "BibleThump": "😢",
    "Sadge": "😔", "monkaS": "😰", "4Head": "😄", "EZ": "😎", "Clap": "👏",
    "<3": "❤️", "HeyGuys": "👋", "catJAM": "🐱", "PepeLaugh": "😆", "Kreygasm": "😩",
    "NotLikeThis": "🤦", "ResidentSleeper": "😴", "SeemsGood": "👍",
    "FeelsGoodMan": "😊", "FeelsBadMan": "😞", "WutFace": "😨", "Jebaited": "😜",
    "HYPERS": "🤩", "peepoHappy": "😊", "LULW": "😂",
}


def emoji_map(settings) -> dict[str, str]:
    """The user's table (a full replacement once edited), else the defaults."""
    custom = settings.get("youtube_emotes.map")
    if isinstance(custom, dict):
        return {str(k): str(v) for k, v in custom.items() if k and v}
    return dict(DEFAULT_EMOJI)


def youtube_emote_text(text: str, registry: EmoteRegistry | None, channel: str | None,
                       settings) -> str:
    """The YouTube copy of `text`: known emote names are removed, or — with
    ``youtube_emotes.emoji`` on — table names become their emoji (unmapped
    emotes are still removed). Whitespace is kept as typed unless a removal
    leaves a gap, which is collapsed. Without a registry nothing is known to
    be an emote, so only the (opt-in) emoji table applies."""
    mapping = emoji_map(settings) if settings.get("youtube_emotes.emoji", False) else {}
    if registry is not None:
        # Split glued Twitch emote names first, exactly as the Twitch copy
        # is respaced: clicking an emote 3x in the picker inserts
        # `redcyb1Hiredcyb1Hiredcyb1Hi`, which Twitch renders as three emotes
        # and which must not leak to YouTube as one unknown word.
        text = registry.space_twitch_emotes(text, channel=channel)
    punct = "".join(PUNCT_STRIP)
    out: list[str] = []
    removed = False
    for token in re.split(r"(\s+)", text):
        if not token or token.isspace():
            out.append(token)
            continue
        # Whole word first: covers table names the registry splitter would
        # break up (`<3` splits into `<` + `3`) and works with no registry.
        if token in mapping:
            out.append(mapping[token])
            continue
        stripped = token.rstrip(punct)
        if stripped != token and stripped in mapping:
            out.append(mapping[stripped] + token[len(stripped):])
            continue
        if registry is None:
            out.append(token)
            continue
        for piece, emote in registry.split_emote_text(token, channel=channel):
            if piece in mapping:
                out.append(mapping[piece])
            elif emote is not None:
                removed = True  # emotes go to Twitch only
            else:
                out.append(piece)
    result = "".join(out)
    if removed:
        result = re.sub(r"\s{2,}", " ", result).strip()
    return result


def has_youtube_text(text: str, registry: EmoteRegistry | None, channel: str | None,
                     settings) -> bool:
    """False when the YouTube copy would be empty (an emote-only message):
    such a message goes to Twitch only."""
    return bool(youtube_emote_text(text, registry, channel, settings).strip())


def emote_runs(text: str, registry: EmoteRegistry | None, channel: str | None) -> list:
    """TextRun/ImageRun pieces for `text`, rendering every known emote name."""
    if registry is None:
        return [TextRun(text)]
    return [
        ImageRun(emote, emote.name) if emote is not None else TextRun(piece)
        for piece, emote in registry.split_emote_text(text, channel=channel)
    ]
