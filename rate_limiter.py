"""
Rate limiter + FloodWait-aware wrapper around a send callable.

Burst pacing model:
  - Send BATCH_SIZE items back-to-back with PER_MESSAGE_DELAY between each
    (default 0.5s → 50 items takes ~25s).
  - After the burst, wait until BATCH_INTERVAL_SEC has elapsed since the
    burst STARTED (default 60s). So if the burst took 25s, we wait 35s;
    if it took 70s (slow due to FloodWait), we don't wait extra.

This gives a steady ~50 items / 60 seconds throughput, which is what
Telegram tolerates for sustained bulk sending to a single chat.

Auto-retries on FloodWait by sleeping the requested duration + small jitter.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Awaitable, Callable, TypeVar

from pyrogram.errors import FloodWait

T = TypeVar("T")


class RateLimiter:
    def __init__(
        self,
        per_message_delay: float = 0.5,
        batch_interval_sec: int = 60,
        max_floodwait_retry: int = 3,
        # Legacy params (kept for backward-compat with old callers; ignored if
        # batch_interval_sec is provided).
        batch_pause_min: int = 0,
        batch_pause_max: int = 0,
    ) -> None:
        self.per_message_delay = max(0.1, per_message_delay)
        # Use the explicit interval if provided; fall back to legacy min/max.
        if batch_interval_sec > 0:
            self.batch_interval_sec = batch_interval_sec
        elif batch_pause_max > 0:
            # Legacy: use midpoint of min/max range.
            self.batch_interval_sec = (batch_pause_min + batch_pause_max) // 2
        else:
            self.batch_interval_sec = 60
        self.max_floodwait_retry = max_floodwait_retry
        self._burst_started_at: float = 0.0  # set by start_burst()

    # ------------------------------------------------------------------
    # Burst lifecycle
    # ------------------------------------------------------------------

    def start_burst(self) -> None:
        """Mark the start of a new burst. batch_pause() will wait until
        batch_interval_sec has elapsed since this call."""
        self._burst_started_at = time.time()

    async def pace(self) -> None:
        """Per-message delay (call BEFORE each send within a burst)."""
        await asyncio.sleep(self.per_message_delay)

    async def batch_pause(self, on_tick=None) -> None:
        """
        Wait until batch_interval_sec has elapsed since start_burst() was called.
        If we've already exceeded that time (burst took long due to FloodWait),
        don't wait extra — just continue.
        """
        if self._burst_started_at == 0:
            # Fallback: if start_burst wasn't called, just sleep the full interval.
            elapsed = 0
            dur = self.batch_interval_sec
        else:
            elapsed = time.time() - self._burst_started_at
            dur = max(0, self.batch_interval_sec - elapsed)

        if dur > 0:
            print(f"[rate] burst cycle: {elapsed:.1f}s elapsed in burst, "
                  f"waiting {dur:.1f}s to hit {self.batch_interval_sec}s target")
            end = time.time() + dur
            while time.time() < end:
                remaining = max(0.0, end - time.time())
                if on_tick is not None:
                    try:
                        on_tick(remaining)
                    except Exception:
                        pass
                await asyncio.sleep(min(1.0, remaining))
        else:
            print(f"[rate] burst took {elapsed:.1f}s (over {self.batch_interval_sec}s target) "
                  f"— no pause needed, continuing immediately")

    # ------------------------------------------------------------------
    # Send with FloodWait retry
    # ------------------------------------------------------------------

    async def send_with_retry(
        self,
        send_fn: Callable[[], Awaitable[T]],
        op_label: str = "send",
    ) -> T:
        """
        Run send_fn() with FloodWait auto-retry.
        Raises FloodWait if exceeded max_floodwait_retry.
        """
        last_err: Exception | None = None
        for attempt in range(1, self.max_floodwait_retry + 1):
            try:
                return await send_fn()
            except FloodWait as e:
                # e.value is seconds Telegram asks us to wait.
                wait = int(e.value) + 2  # +2s slack
                print(f"[rate] FloodWait on {op_label}: must wait {e.value}s "
                      f"(attempt {attempt}/{self.max_floodwait_retry})")
                last_err = e
                # Sleep in chunks so we can be interrupted cleanly.
                end = time.time() + wait
                while time.time() < end:
                    await asyncio.sleep(min(5, end - time.time()))
            except Exception as e:
                # Non-FloodWait errors: log and re-raise immediately.
                print(f"[rate] {op_label} failed with non-retryable error: {e!r}")
                raise
        if last_err:
            raise last_err
        raise RuntimeError(f"{op_label} exhausted retries with no error captured")
