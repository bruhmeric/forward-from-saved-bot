"""
Media-type filtering for Saved Messages.

A message is "matchable" if it carries one of:
  - photo            -> matches filter "photo"
  - video            -> matches filter "video"
  - animation (GIF)  -> matches filter "animation"

Albums (media groups) are considered matchable if ANY item in the group
matches the enabled filter set; the whole album is then forwarded together.
"""
from __future__ import annotations

from typing import Iterable, Set

from pyrogram.types import Message


def message_kind(m: Message) -> str | None:
    """Return 'photo' | 'video' | 'animation' | None."""
    if m is None:
        return None
    if m.photo:
        return "photo"
    if m.animation:  # GIFs arrive as animation in Pyrogram
        return "animation"
    if m.video:
        return "video"
    return None


def is_match(m: Message, enabled: Set[str]) -> bool:
    """True if message matches the enabled filter set AND is forwardable."""
    if m is None:
        return False
    # Service messages, text-only, stickers, voice, documents etc. → skip.
    kind = message_kind(m)
    if kind is None:
        return False
    return kind in enabled


def album_matches(messages: Iterable[Message], enabled: Set[str]) -> bool:
    """True if any message in an album matches the filter."""
    return any(is_match(m, enabled) for m in messages)
