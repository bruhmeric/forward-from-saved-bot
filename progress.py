"""
Progress tracking & live display.

Two layers:

1. **Sweep progress** — how many items have been forwarded in the current
   pass over Saved Messages, with speed (items/min) and ETA.

2. **Per-upload progress** — when an actual file upload happens (fallback
   path: copy_message failed → re-send via send_photo/send_video), Pyrogram
   invokes a `progress` callback with (current, total). We render a live
   percentage bar in the log.

The two layers don't conflict: sweep progress is updated once per item;
upload progress is updated many times per item only when re-upload is needed.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


# ----------------------------------------------------------------------
# Console helpers
# ----------------------------------------------------------------------

def _supports_ansi() -> bool:
    """True if stdout can handle ANSI cursor codes (Render logs do)."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() in ("dumb", ""):
        # Render logs are not a TTY but DO render ANSI.
        return os.environ.get("RENDER", "") == "true" or sys.stdout.isatty()
    return sys.stdout.isatty() or os.environ.get("RENDER") == "true"


ANSI = _supports_ansi()


def _clear_line() -> str:
    return "\r\033[K" if ANSI else "\r"


def _move_up(n: int = 1) -> str:
    return f"\033[{n}A" if ANSI else ""


# ----------------------------------------------------------------------
# ProgressTracker
# ----------------------------------------------------------------------

@dataclass
class ProgressTracker:
    """
    Tracks sweep + batch progress.

    Usage:
        tracker = ProgressTracker()
        tracker.start_sweep(total_estimate=None)   # we don't know total upfront
        # for each item:
        tracker.item_start(item_id, item_kind, n_msgs)
        # if actual upload happens, periodically call:
        tracker.upload_tick(current_bytes, total_bytes)
        # when done:
        tracker.item_done()
        # at batch boundary:
        tracker.batch_pause_start()
        tracker.batch_pause_tick(remaining_seconds)
        tracker.batch_pause_end()
    """
    # Sweep state
    sweep_num: int = 0
    sweep_started_at: float = 0.0
    items_in_sweep: int = 0
    msgs_in_sweep: int = 0             # total messages (albums count as N)
    skipped_in_sweep: int = 0

    # Batch state (batch = group of BATCH_SIZE items)
    items_in_batch: int = 0
    batch_num: int = 0

    # Current item state
    current_item_id: Optional[int] = None
    current_item_kind: str = ""       # "photo"|"video"|"animation"|"album"
    current_item_size: int = 0        # number of messages in unit
    item_started_at: float = 0.0

    # Upload progress (only set during real uploads)
    upload_total: int = 0
    upload_current: int = 0
    upload_active: bool = False
    last_upload_pct: float = -1.0     # throttle log spam

    # Cumulative across all sweeps
    total_items_sent: int = 0
    total_msgs_sent: int = 0
    total_skipped: int = 0
    first_run_at: float = field(default_factory=time.time)

    # Lock for thread-safe console updates (stop watcher thread also writes).
    _print_lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------
    # Sweep lifecycle
    # ------------------------------------------------------------------

    def start_sweep(self) -> None:
        self.sweep_num += 1
        self.sweep_started_at = time.time()
        self.items_in_sweep = 0
        self.msgs_in_sweep = 0
        self.skipped_in_sweep = 0
        self.items_in_batch = 0
        self.batch_num = 0
        self._print(f"\n=== Sweep #{self.sweep_num} started ===")

    def end_sweep(self) -> None:
        dur = time.time() - self.sweep_started_at
        rate = (self.items_in_sweep / dur * 60) if dur > 0 else 0
        self._print(
            f"=== Sweep #{self.sweep_num} done: "
            f"{self.items_in_sweep} items / {self.msgs_in_sweep} msgs in {dur:.1f}s "
            f"({rate:.1f} items/min, {self.skipped_in_sweep} skipped) ===\n"
        )

    # ------------------------------------------------------------------
    # Item lifecycle
    # ------------------------------------------------------------------

    def item_start(self, item_id: int, kind: str, n_msgs: int = 1) -> None:
        self.current_item_id = item_id
        self.current_item_kind = kind
        self.current_item_size = n_msgs
        self.item_started_at = time.time()
        self.upload_active = False
        self.upload_total = 0
        self.upload_current = 0
        self.last_upload_pct = -1.0
        self._render_item_line(status="sending")

    def upload_tick(self, current: int, total: int) -> None:
        """Called by Pyrogram's progress callback during actual uploads."""
        self.upload_active = True
        self.upload_current = current
        self.upload_total = total
        pct = (current / total * 100) if total else 0
        # Throttle: only re-render every 5% to avoid log spam.
        if pct - self.last_upload_pct >= 5 or pct >= 100:
            self.last_upload_pct = pct
            self._render_item_line(status="uploading")

    def item_done(self) -> None:
        dur = time.time() - self.item_started_at
        self.items_in_sweep += 1
        self.msgs_in_sweep += self.current_item_size
        self.total_items_sent += 1
        self.total_msgs_sent += self.current_item_size
        self.items_in_batch += 1
        self._render_item_line(status="done", dur=dur)
        # Newline after a completed item so the next line is fresh.
        sys.stdout.write("\n")
        sys.stdout.flush()
        self.current_item_id = None

    def item_skipped(self, item_id: int, reason: str) -> None:
        self.skipped_in_sweep += 1
        self.total_skipped += 1
        self._print(f"  ↪ skip msg_id={item_id}: {reason}")

    # ------------------------------------------------------------------
    # Batch pause lifecycle
    # ------------------------------------------------------------------

    def batch_pause_start(self) -> None:
        self.batch_num += 1
        self._print(f"  ⏸  Batch #{self.batch_num} complete — pausing…")

    def batch_pause_tick(self, remaining_sec: float) -> None:
        # Render in-place; carriage-return overwrites the previous tick.
        bar = self._mini_bar(1 - remaining_sec / max(1, remaining_sec + 1))
        sys.stdout.write(f"\r  ⏸  Batch pause {bar} {remaining_sec:.0f}s remaining   ")
        sys.stdout.flush()

    def batch_pause_end(self) -> None:
        sys.stdout.write("\r" + (" " * 60) + "\r")
        sys.stdout.flush()
        self._print("  ▶  Resuming after batch pause.")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self) -> str:
        dur = time.time() - self.first_run_at
        rate = (self.total_items_sent / dur * 60) if dur > 0 else 0
        return (
            f"\n--- Cumulative ---\n"
            f"  sweeps:        {self.sweep_num}\n"
            f"  items sent:    {self.total_items_sent}\n"
            f"  messages sent: {self.total_msgs_sent}\n"
            f"  skipped:       {self.total_skipped}\n"
            f"  runtime:       {dur:.0f}s ({rate:.1f} items/min avg)\n"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _render_item_line(self, status: str, dur: Optional[float] = None) -> None:
        """Single-line, in-place rendering of the current item."""
        if self.current_item_id is None:
            return
        # Running totals
        cum_items = self.total_items_sent + (1 if status == "done" else 0)
        cum_msgs = self.total_msgs_sent + (self.current_item_size if status == "done" else 0)

        # Per-sweep
        sweep_items = self.items_in_sweep + (1 if status == "done" else 0)
        elapsed = time.time() - self.sweep_started_at
        sweep_rate = (sweep_items / elapsed * 60) if elapsed > 0 else 0

        kind = self.current_item_kind
        if self.current_item_size > 1:
            kind = f"album({self.current_item_size}×{kind})"

        # Upload progress bar (if active)
        if self.upload_active and self.upload_total:
            pct = self.upload_current / self.upload_total * 100
            bar = self._mini_bar(pct / 100)
            upload_str = f" upload {bar} {pct:5.1f}%"
        else:
            upload_str = ""

        # Status emoji
        emoji = {"sending": "→", "uploading": "↑", "done": "✓"}.get(status, "·")

        dur_str = f" ({dur:.1f}s)" if dur is not None else ""

        line = (
            f"{emoji} sweep#{self.sweep_num} item#{sweep_items} "
            f"msg_id={self.current_item_id} [{kind}]{upload_str}"
            f" — cumulative {cum_items} items / {cum_msgs} msgs "
            f"({sweep_rate:.1f}/min){dur_str}"
        )
        # Truncate to terminal width (Render logs are wide, but be safe).
        max_w = 200
        if len(line) > max_w:
            line = line[: max_w - 1] + "…"

        with self._print_lock:
            sys.stdout.write(_clear_line() + line)
            sys.stdout.flush()

    def _mini_bar(self, ratio: float, width: int = 12) -> str:
        ratio = max(0.0, min(1.0, ratio))
        filled = int(ratio * width)
        return "[" + "█" * filled + "░" * (width - filled) + "]"

    def _print(self, msg: str) -> None:
        with self._print_lock:
            # Clear any in-progress line first so we don't get a mess.
            sys.stdout.write(_clear_line())
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()


# ----------------------------------------------------------------------
# Pyrogram progress adapter
# ----------------------------------------------------------------------

def make_upload_callback(tracker: ProgressTracker, loop: asyncio.AbstractEventLoop):
    """
    Build a sync `progress(current, total)` callback for Pyrogram's send_*.
    Pyrogram calls it from a worker thread — we marshal to the asyncio loop
    so tracker state stays consistent.
    """
    def _cb(current: int, total: int) -> None:
        # Pyrogram's progress callback is synchronous; bridge to async loop.
        try:
            asyncio.run_coroutine_threadsafe(
                _async_update(tracker, current, total), loop
            ).result(timeout=1)
        except Exception:
            # Fallback: direct sync call (best-effort).
            tracker.upload_tick(current, total)
    return _cb


async def _async_update(tracker: ProgressTracker, current: int, total: int) -> None:
    tracker.upload_tick(current, total)
