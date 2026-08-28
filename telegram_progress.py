"""
Telegram-side live progress reporter.

Posts a single "📊 progress" message to the configured chat (defaults to
your Saved Messages) and keeps editing it in-place as the bot runs.

Throttled to one edit per `progress_update_interval` seconds (default 5s)
to avoid Telegram's ~30 edits/minute FloodWait.

State machine:
  start()      → post initial message, persist its message_id
  update()     → throttle-check, then edit_message_text
  force_update() → bypass throttle (use sparingly: sweep end, stop, etc.)
  status_snapshot() → return a one-shot text string (for /status replies)
  stop()       → final update with "stopped" status

The message_id is persisted to `progress_message_id_file` so a restart
edits the same message rather than spamming a new one.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid importing telethon at module load — TelegramProgress is constructed
    # with a client object, but we don't actually use any telethon-specific
    # types in our annotations. This keeps the module importable even if
    # telethon isn't installed (e.g. during development).
    from telethon import TelegramClient


@dataclass
class Snapshot:
    """A point-in-time view of the bot's progress, used to render the message."""
    # Identity / config (constant)
    target: str = ""
    filter_types: set = field(default_factory=set)
    order: str = ""

    # Sweep
    sweep_num: int = 0
    sweep_started_at: float = 0.0
    items_in_sweep: int = 0
    msgs_in_sweep: int = 0
    skipped_in_sweep: int = 0

    # Cumulative
    total_items_sent: int = 0
    total_msgs_sent: int = 0
    total_skipped: int = 0
    first_run_at: float = field(default_factory=time.time)

    # Current item
    current_item_id: Optional[int] = None
    current_item_kind: str = ""
    current_item_size: int = 0
    item_started_at: float = 0.0

    # Upload
    upload_active: bool = False
    upload_current: int = 0
    upload_total: int = 0

    # Batch pause
    batch_pause_active: bool = False
    batch_pause_remaining: float = 0.0
    batch_pause_total: float = 0.0
    batch_num: int = 0

    # Lifecycle
    stopped: bool = False
    stop_reason: str = ""


class TelegramProgress:
    """
    Owns the live progress message in Telegram.

    Usage:
        tp = TelegramProgress(client, cfg)
        await tp.start()
        # ... on every state change ...
        await tp.update(snapshot)         # throttled
        # ... on key events ...
        await tp.force_update(snapshot)   # bypasses throttle
        await tp.stop(snapshot)            # final message
    """

    def __init__(
        self,
        client: Client,
        chat: str,
        update_interval: float = 5.0,
        message_id_file: str = "",
    ) -> None:
        self.client = client
        self.chat = chat
        self.update_interval = max(2.0, update_interval)
        self.message_id_file = message_id_file

        self.message_id: Optional[int] = None
        self._last_edit_at: float = 0.0
        self._edit_lock = asyncio.Lock()
        self._enabled = True  # toggle via disable()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_message_id(self) -> Optional[int]:
        if not self.message_id_file:
            return None
        try:
            with open(self.message_id_file, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            return int(raw) if raw else None
        except (FileNotFoundError, ValueError):
            return None
        except Exception as e:
            print(f"[tg-progress] could not load message_id file: {e!r}")
            return None

    def _save_message_id(self, mid: int) -> None:
        if not self.message_id_file:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.message_id_file)) or ".", exist_ok=True)
            with open(self.message_id_file, "w", encoding="utf-8") as f:
                f.write(str(mid))
        except Exception as e:
            print(f"[tg-progress] could not save message_id file: {e!r}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, initial: Snapshot) -> None:
        """Either reuse an existing progress message or post a new one."""
        if not self._enabled:
            # Disabled — skip silently. Web UI is the only progress surface.
            return
        # Try to reuse a previously-persisted message_id.
        existing = self._load_message_id()
        if existing:
            # Probe by trying to edit it. If it fails (deleted / too old / different chat),
            # we'll fall through and post a new one.
            try:
                await self.client.edit_message(self.chat, existing, self.render(initial))
                self.message_id = existing
                print(f"[tg-progress] reusing message_id={existing}")
                return
            except Exception as e:
                print(f"[tg-progress] could not reuse message_id={existing}: {e!r}; posting new one.")

        # Post fresh.
        try:
            sent = await self.client.send_message(self.chat, self.render(initial), link_preview=False)
            # Telethon's send_message returns a single Message object.
            mid = sent.id if sent else None
            self.message_id = mid
            self._save_message_id(mid)
            print(f"[tg-progress] posted live progress message (id={mid}) to chat={self.chat!r}")
        except Exception as e:
            print(f"[tg-progress] could not post progress message: {e!r}; progress disabled")
            self._enabled = False

    async def update(self, snap: Snapshot) -> None:
        """Throttled update. Safe to call from hot loops."""
        if not self._enabled or self.message_id is None:
            return
        now = time.time()
        if now - self._last_edit_at < self.update_interval:
            return
        await self._do_edit(snap)

    async def force_update(self, snap: Snapshot) -> None:
        """Bypass throttle. Use sparingly (max ~1/sec)."""
        if not self._enabled or self.message_id is None:
            return
        await self._do_edit(snap)

    async def _do_edit(self, snap: Snapshot) -> None:
        async with self._edit_lock:
            try:
                await self.client.edit_message(self.chat, self.message_id, self.render(snap), link_preview=False)
                self._last_edit_at = time.time()
            except Exception as e:
                # If the message was deleted or chat is gone, disable silently.
                msg = str(e)
                if "MESSAGE_ID_INVALID" in msg or "MESSAGE_NOT_MODIFIED" in msg:
                    if "MESSAGE_NOT_MODIFIED" in msg:
                        # Not actually an error — content unchanged. Update timestamp.
                        self._last_edit_at = time.time()
                        return
                    print(f"[tg-progress] message gone ({e!r}); progress disabled")
                    self._enabled = False
                else:
                    # Don't spam — just log and back off this round.
                    print(f"[tg-progress] edit failed: {e!r}")
                    self._last_edit_at = time.time()  # still counts as an attempt

    async def stop(self, snap: Snapshot) -> None:
        """Final update with the 'stopped' banner."""
        if not self._enabled or self.message_id is None:
            return
        snap.stopped = True
        await self._do_edit(snap)

    def disable(self) -> None:
        self._enabled = False

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, s: Snapshot) -> str:
        """Format the snapshot as a Telegram-friendly monospaced block."""
        now = time.time()

        # ----- Header -----
        status_emoji = "🛑" if s.stopped else ("⏸" if s.batch_pause_active else "▶️")
        status_text = "STOPPED" if s.stopped else ("PAUSED" if s.batch_pause_active else "RUNNING")
        lines = [
            f"{status_emoji} <b>Bulk Forwarder — Live Progress</b>",
            f"<i>Status:</i> <b>{status_text}</b>"
            + (f" · {s.stop_reason}" if s.stop_reason else ""),
            "━━━━━━━━━━━━━━━━━━━━━━━━",
        ]

        # ----- Config line -----
        filter_str = ",".join(sorted(s.filter_types)) if s.filter_types else "—"
        lines.append(f"📍 <b>Target:</b> {s.target}")
        lines.append(f"📍 <b>Filter:</b> {filter_str}  ·  <b>Order:</b> {s.order}")

        # ----- Sweep progress -----
        if s.sweep_num > 0:
            sweep_elapsed = now - s.sweep_started_at if s.sweep_started_at else 0
            sweep_rate = (s.items_in_sweep / sweep_elapsed * 60) if sweep_elapsed > 0 else 0
            lines.append("")
            lines.append(f"🔄 <b>Sweep #{s.sweep_num}</b> · {self._fmt_dur(sweep_elapsed)} elapsed")
            lines.append(f"   📦 <b>{s.items_in_sweep}</b> items · <b>{s.msgs_in_sweep}</b> msgs · "
                         f"<b>{s.skipped_in_sweep}</b> skipped")
            lines.append(f"   ⚡ <b>{sweep_rate:.1f}</b> items/min")

        # ----- Cumulative -----
        total_dur = now - s.first_run_at if s.first_run_at else 0
        total_rate = (s.total_items_sent / total_dur * 60) if total_dur > 0 else 0
        lines.append("")
        lines.append(f"📊 <b>Cumulative</b>")
        lines.append(f"   📦 <b>{s.total_items_sent}</b> items · <b>{s.total_msgs_sent}</b> msgs · "
                     f"<b>{s.total_skipped}</b> skipped")
        lines.append(f"   ⏱ <b>{self._fmt_dur(total_dur)}</b> runtime · "
                     f"<b>{total_rate:.1f}</b> items/min avg")

        # ----- Current item -----
        if s.current_item_id is not None and not s.batch_pause_active:
            kind = s.current_item_kind
            if s.current_item_size > 1:
                kind = f"album({s.current_item_size}×{kind})"
            item_elapsed = now - s.item_started_at if s.item_started_at else 0
            lines.append("")
            lines.append(f"🔄 <b>Current item</b>")
            lines.append(f"   msg_id=<code>{s.current_item_id}</code> · [{kind}]")
            lines.append(f"   ⏱ {item_elapsed:.1f}s elapsed")
            if s.upload_active and s.upload_total:
                pct = s.upload_current / s.upload_total * 100
                bar = self._mini_bar(pct / 100)
                cur_mb = s.upload_current / (1024 * 1024)
                tot_mb = s.upload_total / (1024 * 1024)
                lines.append(f"   ↑ Upload {bar} {pct:5.1f}% "
                             f"({cur_mb:.1f}/{tot_mb:.1f} MB)")

        # ----- Batch pause -----
        if s.batch_pause_active:
            pct_done = 1 - (s.batch_pause_remaining / max(1, s.batch_pause_total))
            bar = self._mini_bar(pct_done)
            lines.append("")
            lines.append(f"⏸ <b>Batch #{s.batch_num} pause</b>")
            lines.append(f"   {bar} {s.batch_pause_remaining:.0f}s remaining "
                         f"(of {s.batch_pause_total:.0f}s)")

        # ----- Footer -----
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        lines.append(f"🕒 Last update: {ts}")
        if not s.stopped:
            lines.append("💬 Send <code>/stop</code> to halt · <code>/status</code> for snapshot")

        return "\n".join(lines)

    def status_snapshot_text(self, s: Snapshot) -> str:
        """A compact one-shot snapshot for /status command replies."""
        now = time.time()
        total_dur = now - s.first_run_at if s.first_run_at else 0
        total_rate = (s.total_items_sent / total_dur * 60) if total_dur > 0 else 0
        sweep_rate = 0.0
        if s.sweep_started_at:
            sweep_elapsed = now - s.sweep_started_at
            sweep_rate = (s.items_in_sweep / sweep_elapsed * 60) if sweep_elapsed > 0 else 0
        return (
            f"📊 <b>Status snapshot</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Target: <code>{s.target}</code>\n"
            f"Filter: <code>{','.join(sorted(s.filter_types))}</code>\n"
            f"Sweep: #{s.sweep_num} ({s.items_in_sweep} items, {s.skipped_in_sweep} skipped, {sweep_rate:.1f}/min)\n"
            f"Total: {s.total_items_sent} items / {s.total_msgs_sent} msgs ({total_rate:.1f}/min over {self._fmt_dur(total_dur)})\n"
            + (f"Current: msg_id={s.current_item_id} [{s.current_item_kind}]\n" if s.current_item_id else "Current: idle\n")
            + (f"Upload: {s.upload_current/(1024*1024):.1f}/{s.upload_total/(1024*1024):.1f} MB\n" if s.upload_active and s.upload_total else "")
            + ("Status: 🛑 STOPPED" if s.stopped else "Status: ▶️ RUNNING")
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_dur(sec: float) -> str:
        sec = int(sec)
        if sec < 60:
            return f"{sec}s"
        if sec < 3600:
            return f"{sec // 60}m{sec % 60}s"
        h = sec // 3600
        m = (sec % 3600) // 60
        return f"{h}h{m}m"

    @staticmethod
    def _mini_bar(ratio: float, width: int = 10) -> str:
        ratio = max(0.0, min(1.0, ratio))
        filled = int(ratio * width)
        return "[" + "█" * filled + "░" * (width - filled) + "]"
