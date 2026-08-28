"""
Telegram-synced state persistence — Telethon version.

On Render's free tier, there's no persistent disk — `state.json` gets wiped
on every redeploy, sleep cycle, or service restart. To work around this,
we mirror the state to a Telegram chat (default: Saved Messages) by posting
the JSON as a document message tagged with a magic prefix.

Flow:
  - On startup: search the control chat for the latest "[BULK-FORWARDER-STATE]"
    document message, download it, and seed State.sent_ids.
  - After every N items sent (or every M minutes), re-post the current state
    as a fresh document, then delete the previous one to keep the chat clean.

If both local state.json AND Telegram state exist, Telegram wins (it's the
more durable copy on free tier).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from telethon import TelegramClient

# Magic prefix that identifies our state-sync document in the chat.
# Stored as the message caption.
STATE_TAG = "[BULK-FORWARDER-STATE]"
STATE_TAG_RE = re.compile(r"\[BULK-FORWARDER-STATE\]", re.IGNORECASE)


class TelegramStateSync:
    """
    Persist `state.json` to a Telegram chat so it survives free-tier redeploys.

    Usage:
        tss = TelegramStateSync(client=client, chat="me", target="@mychannel")
        await tss.bootstrap()                    # pull latest state from chat
        # ... later, periodically ...
        await tss.sync(sent_ids=set([1,2,3]))     # push updated state
    """

    def __init__(
        self,
        client: TelegramClient,
        chat: str = "me",
        target: str = "",
        max_history_messages: int = 50,
    ) -> None:
        self.client = client
        self.chat = chat
        self.target = target
        self.max_history_messages = max_history_messages
        # Cached message_id of the latest state document, so we can delete it
        # when posting a new one.
        self._latest_state_msg_id: Optional[int] = None

    # ------------------------------------------------------------------
    # Bootstrap: load state from Telegram on startup
    # ------------------------------------------------------------------

    async def bootstrap(self) -> set[int]:
        """
        Search the control chat for the latest state-sync document.
        Returns the set of sent_ids it contains (empty if none found).
        """
        print(f"[state-sync] scanning {self.chat!r} for latest state document…")
        try:
            async for msg in self.client.iter_messages(self.chat, limit=self.max_history_messages):
                if not self._is_state_message(msg):
                    continue
                # Found the latest one.
                self._latest_state_msg_id = msg.id
                sent_ids = await self._download_state_from(msg)
                if sent_ids is None:
                    continue  # corrupt; try the next one
                print(f"[state-sync] found state doc msg_id={msg.id} "
                      f"with {len(sent_ids)} sent_ids")
                return sent_ids
        except Exception as e:
            print(f"[state-sync] bootstrap failed: {e!r}")
        print(f"[state-sync] no prior state document found; starting fresh")
        return set()

    def _is_state_message(self, msg) -> bool:
        if msg is None:
            return False
        # Telethon: msg.document is None for non-document messages.
        if not getattr(msg, "document", None):
            # Also accept text messages with the tag (legacy / very small state).
            text = getattr(msg, "message", None) or ""
            if STATE_TAG_RE.search(text):
                return True
            return False
        # msg.message in Telethon is the caption (or text).
        caption = getattr(msg, "message", None) or ""
        if not STATE_TAG_RE.search(caption):
            return False
        return True

    async def _download_state_from(self, msg) -> Optional[set[int]]:
        """Download and parse the state JSON from a state-sync message."""
        try:
            buf = await self.client.download_media(msg, file=bytes)
            if buf is None:
                return None
            # Telethon returns bytes when file=bytes is specified.
            if isinstance(buf, (bytes, bytearray)):
                data_bytes = bytes(buf)
            elif hasattr(buf, "read"):
                data_bytes = buf.read()
            else:
                data_bytes = open(buf, "rb").read()
            data = json.loads(data_bytes.decode("utf-8"))
            # Target mismatch → discard (different destination).
            if self.target and data.get("target") and data["target"] != self.target:
                print(f"[state-sync] target mismatch "
                      f"({data.get('target')!r} ≠ {self.target!r}); ignoring")
                return None
            ids = set(int(x) for x in data.get("sent_ids", []))
            return ids
        except Exception as e:
            print(f"[state-sync] could not parse state doc msg_id={msg.id}: {e!r}")
            return None

    # ------------------------------------------------------------------
    # Sync: push current state to Telegram
    # ------------------------------------------------------------------

    async def sync(self, sent_ids: set[int]) -> None:
        """
        Post the current state as a new document; delete the previous one.
        Throttle: caller should rate-limit (e.g., once per minute).
        """
        if not self.client.is_connected():
            return

        data = {
            "schema_version": 1,
            "target": self.target,
            "sent_ids": sorted(int(x) for x in sent_ids),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(sent_ids),
        }
        try:
            data_bytes = json.dumps(data, indent=2).encode("utf-8")
            caption = (
                f"{STATE_TAG}\n"
                f"target={self.target}\n"
                f"sent_ids={len(sent_ids)}\n"
                f"updated={data['updated_at']}"
            )
            # Telethon: send_file with file=bytes uploads in-memory bytes.
            # force_document=True ensures it's sent as a generic file, not a photo.
            sent = await self.client.send_file(
                self.chat,
                file=data_bytes,
                force_document=True,
                caption=caption,
            )
            new_id = sent.id if sent else None
            # Delete previous one (if any).
            if self._latest_state_msg_id is not None and self._latest_state_msg_id != new_id:
                try:
                    await self.client.delete_messages(self.chat, [self._latest_state_msg_id])
                except Exception as e:
                    print(f"[state-sync] could not delete previous state msg_id={self._latest_state_msg_id}: {e!r}")
            self._latest_state_msg_id = new_id
            print(f"[state-sync] pushed state doc msg_id={new_id} ({len(sent_ids)} ids)")
        except Exception as e:
            print(f"[state-sync] sync failed: {e!r}")
