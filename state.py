"""
state.json persistence for "already-sent message IDs".
Supports resume after restart/crash.

Schema:
{
  "target": "@somechannel",          // last target this state belongs to
  "sent_ids": [101, 102, 103, ...]  // Saved Messages message_ids already forwarded
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
            if data.get("target"):
                self.target = data["target"]
            print(f"[state] loaded {len(self.sent_ids)} already-sent IDs from {self.path}")
        except Exception as e:
            print(f"[state] WARNING failed to load {self.path}: {e}; starting fresh.")

    def save(self) -> None:
        """Atomic write: write to tmp file then rename."""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        data = {
            "target": self.target,
            "sent_ids": sorted(self.sent_ids),
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

    def mark_sent_many(self, ids: Iterable[int]) -> None:
        for x in ids:
            self.sent_ids.add(int(x))

    def was_sent(self, msg_id: int) -> bool:
        return int(msg_id) in self.sent_ids

    def __len__(self) -> int:
        return len(self.sent_ids)
