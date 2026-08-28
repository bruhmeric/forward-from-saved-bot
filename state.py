"""
state.json persistence for "already-sent message IDs" + "last offset watermark".
Supports resume after restart/crash.

Schema:
{
  "target": "@somechannel",            // last target this state belongs to
  "sent_ids": [101, 102, 103, ...],  // Saved Messages message_ids already forwarded
  "last_offset_id": 4839,            // highest Saved Messages id processed (for ORDER=old auto-resume)
  "schema_version": 2
}
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Iterable, Set

# Where state.json lives. Override via STATE_FILE env var.
DEFAULT_STATE_FILE = "./state.json"


class State:
    def __init__(self, path: str = DEFAULT_STATE_FILE, target: str = "") -> None:
        self.path = path
        self.target = target
        self.sent_ids: Set[int] = set()
        self.last_offset_id: int = 0   # highest Saved Messages id processed (for auto-resume)
        self._load()

    # --- I/O ----------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # If target differs, ignore previous state (different destination).
            if self.target and data.get("target") and data["target"] != self.target:
                print(f"[state] target changed ({data.get('target')} → {self.target}); starting fresh.")
                return
            self.sent_ids = set(int(x) for x in data.get("sent_ids", []))
            self.last_offset_id = int(data.get("last_offset_id", 0) or 0)
            if data.get("target"):
                self.target = data["target"]
            print(f"[state] loaded {len(self.sent_ids)} already-sent IDs "
                  f"(last_offset_id={self.last_offset_id}) from {self.path}")
        except Exception as e:
            print(f"[state] WARNING failed to load {self.path}: {e}; starting fresh.")

    def save(self) -> None:
        """Atomic write: write to tmp file then rename."""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        data = {
            "schema_version": 2,
            "target": self.target,
            "sent_ids": sorted(self.sent_ids),
            "last_offset_id": self.last_offset_id,
        }
        # Atomic write to avoid corruption on crash.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)) or ".",
                                    prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise

    # --- API ----------------------------------------------------------------

    def mark_sent(self, msg_id: int) -> None:
        self.sent_ids.add(int(msg_id))
        # Track the highest message_id we've processed (for auto-resume).
        if int(msg_id) > self.last_offset_id:
            self.last_offset_id = int(msg_id)

    def mark_sent_many(self, ids: Iterable[int]) -> None:
        for x in ids:
            x = int(x)
            self.sent_ids.add(x)
            if x > self.last_offset_id:
                self.last_offset_id = x

    def update_offset_id(self, msg_id: int) -> None:
        """Update last_offset_id without marking as sent (for skipped items)."""
        if int(msg_id) > self.last_offset_id:
            self.last_offset_id = int(msg_id)

    def was_sent(self, msg_id: int) -> bool:
        return int(msg_id) in self.sent_ids

    def reset_offset(self) -> None:
        """Reset the offset watermark (keeps sent_ids).

        Use this if you want to re-scan from the beginning on the next sweep
        without losing the 'already sent' history.
        """
        self.last_offset_id = 0

    def __len__(self) -> int:
        return len(self.sent_ids)
