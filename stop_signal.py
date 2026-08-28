"""
Stop-signal + status-command watcher.

Polls the user's Saved Messages (or another control chat) for:
  /stop, /halt, /kill    → flip the stop_event
  /status                → reply with a one-shot status snapshot

When detected, sets a threading.Event that the main loop checks before
sending each item.
"""
from __future__ import annotations

import asyncio
import re
import threading
from typing import Callable, Optional

from pyrogram import Client

# Strip surrounding whitespace, optional leading slash; allow /stop, /halt, /kill
STOP_RE = re.compile(r"^\s*/?\s*(stop|halt|kill)\b", re.IGNORECASE)
STATUS_RE = re.compile(r"^\s*/?\s*status\b", re.IGNORECASE)

# Signature on a /status reply so we can edit it later if we want.
STATUS_REPLY_PREFIX = "📊 Status snapshot"


class StopWatcher:
    def __init__(
        self,
        client: Client,
        poll_interval: int = 5,
        control_chat: str = "me",
    ) -> None:
        self.client = client
        self.poll_interval = max(2, poll_interval)
        self.control_chat = control_chat  # where to look for /stop and /status
        self.stop_event = threading.Event()
        self._task: Optional[asyncio.Task] = None
        self._started_at: Optional[int] = None  # Telegram message_id at start time
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Callback invoked when /status is received. Returns a string to send
        # back as the reply. The callback runs on the watcher's event loop.
        self.status_callback: Optional[Callable[[], str]] = None

    # --- Public API ---------------------------------------------------------

    def start_background(self) -> None:
        """Start the watcher in a daemon thread with its own event loop."""
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="StopWatcher")
        self._thread.start()

    def stop_requested(self) -> bool:
        return self.stop_event.is_set()

    def request_stop(self) -> None:
        """Programmatic stop (e.g., from Ctrl+C handler)."""
        self.stop_event.set()

    # --- Internals ----------------------------------------------------------

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception as e:
            print(f"[stop] watcher crashed: {e!r}")
        finally:
            self._loop.close()

    async def _run(self) -> None:
        # Wait a moment so the main client is up before we start polling.
        await asyncio.sleep(2)
        # Record the latest message_id at start so we ignore historical commands.
        try:
            async for m in self.client.get_chat_history(self.control_chat, limit=1):
                self._started_at = m.id
                break
        except Exception as e:
            print(f"[stop] could not read current {self.control_chat!r} head: {e!r}")
            self._started_at = 0

        print(f"[stop] watching {self.control_chat!r} for /stop and /status "
              f"(watermark msg_id={self._started_at})")

        while not self.stop_event.is_set():
            try:
                await self._poll_once()
            except Exception as e:
                print(f"[stop] poll error: {e!r}")
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self) -> None:
        # Look at the last ~10 messages in the control chat. Commands are recent.
        async for m in self.client.get_chat_history(self.control_chat, limit=10):
            # Only react to messages newer than our start watermark.
            if self._started_at and m.id <= self._started_at:
                continue

            text = (m.text or "").strip()
            if not text:
                continue

            if STOP_RE.match(text):
                print(f"[stop] received stop command from msg_id={m.id}: {text!r}")
                self.stop_event.set()
                try:
                    await self.client.edit_message_text(
                        self.control_chat, m.id,
                        "🛑 Stop signal received. The bot will halt after the current item.",
                    )
                except Exception:
                    pass
                return

            if STATUS_RE.match(text):
                print(f"[status] received /status command from msg_id={m.id}")
                # Mark command as seen so we don't re-react (edit it).
                try:
                    await self.client.edit_message_text(
                        self.control_chat, m.id,
                        "📊 Generating status snapshot…",
                    )
                except Exception:
                    pass

                # Run the status callback (it returns a string).
                reply_text = "📊 (no status callback registered)"
                if self.status_callback is not None:
                    try:
                        reply_text = self.status_callback()
                    except Exception as e:
                        reply_text = f"📊 Status callback failed: {e!r}"

                # Send the reply as a new message in the control chat.
                try:
                    await self.client.send_message(self.control_chat, reply_text)
                except Exception as e:
                    print(f"[status] could not send reply: {e!r}")
                return
