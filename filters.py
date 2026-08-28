"""
Media-type filtering for Saved Messages.

Telethon's message objects expose:
  - msg.photo      → MessageMediaPhoto
  - msg.video      → MessageMediaDocument with video mime type
  - msg.gif        → MessageMediaDocument with gif/animation mime type
  - msg.document    → other documents (skip)

A message is "matchable" if it carries one of:
  - photo            -> matches filter "photo"
  - video            -> matches filter "video"
  - gif (animation)  -> matches filter "animation"

Albums (media groups) are considered matchable if ANY item in the group
matches the enabled filter set; the whole album is then forwarded together.
"""
from __future__ import annotations

from typing import Iterable, Set


def message_kind(m) -> str | None:
    """Return 'photo' | 'video' | 'animation' | None."""
    if m is None:
        return None
    if getattr(m, "photo", None):
        return "photo"
    if getattr(m, "gif", None):
        return "animation"
    if getattr(m, "video", None):
        return "video"
    return None


def is_match(m, enabled: Set[str]) -> bool:
    """True if message matches the enabled filter set AND is forwardable."""
    if m is None:
        return False
    kind = message_kind(m)
    if kind is None:
        return False
    return kind in enabled


def album_matches(messages: Iterable, enabled: Set[str]) -> bool:
    """True if any message in an album matches the filter."""
    return any(is_match(m, enabled) for m in messages)
