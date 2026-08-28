"""
Stop-signal handler — minimal asyncio.Event wrapper.

This used to poll the user's Saved Messages for /stop & /status commands.
That code was REMOVED because it caused "Task got Future attached to a
different loop" errors when running on Render (Pyrogram's loop vs. our loop).

The web UI at POST /stop is now the ONLY control surface. SIGINT/SIGTERM
handlers also flip the same asyncio.Event via loop.call_soon_threadsafe().

If you want Telegram-side control back, see git history for the polling
implementation — but you'll need to fix the cross-loop issue first.
"""
from __future__ import annotations

import asyncio
from typing import Optional


class StopWatcher:
    """
    Owns the asyncio.Event that signals graceful shutdown.

    No background task, no Saved Messages polling, no Pyrogram calls.
    Just a thread-safe-ish flag that the signal handler and web /stop
    endpoint can flip from anywhere.
    """
    def __init__(self, **_kwargs) -> None:
        """
        Accepts (and ignores) the old kwargs (client, poll_interval,
        control_chat, poll_control_chat) for backwards compatibility
        with main.py — but doesn't use any of them.
        """
        self.stop_event = asyncio.Event()

    # --- Public API ---------------------------------------------------------

    def start(self) -> None:
        """No-op. Kept for API compatibility with old main.py."""
        pass

    def stop_requested(self) -> bool:
        return self.stop_event.is_set()

    def request_stop(self) -> None:
        """Programmatic stop (signal handler, web /stop, etc.)."""
        self.stop_event.set()

    async def stop(self) -> None:
        """No-op. Kept for API compatibility with old main.py."""
        pass
