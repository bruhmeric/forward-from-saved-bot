"""
Rate limiter + FloodWait-aware wrapper around a send callable.

Conservative profile: ~2-3s between messages, longer pause after each batch.
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
        per_message_delay: float = 2.5,
        batch_pause_min: int = 120,
        batch_pause_max: int = 180,
        max_floodwait_retry: int = 3,
    ) -> None:
        self.per_message_delay = max(0.2, per_message_delay)
        self.batch_pause_min = batch_pause_min
        self.batch_pause_max = max(batch_pause_min, batch_pause_max)
        self.max_floodwait_retry = max_floodwait_retry

    async def pace(self) -> None:
        """Per-message delay (call BEFORE each send)."""
        await asyncio.sleep(self.per_message_delay)

    async def batch_pause(self, on_tick=None) -> None:
        """
        Pause after each batch of N.
        Optional `on_tick(remaining_sec)` callback receives remaining seconds
        every 1s — used by ProgressTracker.batch_pause_tick() to render a
        live countdown bar.
        """
        dur = random.randint(self.batch_pause_min, self.batch_pause_max)
        print(f"[rate] batch pause {dur}s to respect Telegram limits…")
        # Sleep in small increments so a stop signal can interrupt faster.
        end = time.time() + dur
        while time.time() < end:
            remaining = max(0.0, end - time.time())
            if on_tick is not None:
                try:
                    on_tick(remaining)
                except Exception:
                    pass
            await asyncio.sleep(min(1.0, remaining))

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
                print(f"[rate] FloodWait on {op_label}: must wait {e.value}s (attempt {attempt}/{self.max_floodwait_retry})")
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
