"""
Core forwarding logic — Telethon version.

Key simplifications vs the Pyrogram version:
  - iter_units uses client.iter_messages("me", filter=..., reverse=True, min_id=...)
    which is a SINGLE ASYNC ITERATOR that:
      * Filters server-side (no client-side filter needed)
      * Returns oldest-first (no manual pagination + reverse-in-memory dance)
      * Respects min_id for auto-resume watermark
      * Streams (no need to load everything into RAM)
  - Forwards batches via client.forward_messages(target, batch,
    drop_author=True, drop_media_captions=True) which:
      * Sends up to 100 messages in a single MTProto call
      * Strips the caption AND the "forwarded from" header (drops the forward tag)
      * Preserves album grouping if the whole album is in the batch

This is dramatically simpler than the Pyrogram version.
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, List, Tuple

from telethon import TelegramClient
from telethon.tl.types import (
    InputMessagesFilterPhotos,
    InputMessagesFilterVideo,
    InputMessagesFilterGif,
    InputMessagesFilterEmpty,
)
from telethon.tl.custom.message import Message as TelethonMessage

from config import Config
from filters import is_match, message_kind
from progress import ProgressTracker, make_upload_callback
from rate_limiter import RateLimiter
from state import State
from stop_signal import StopWatcher
from telegram_progress import Snapshot, TelegramProgress
from web_server import WebServer

# Map our filter type names to Telethon's InputMessagesFilter types.
# Note: when filtering by multiple media types, Telethon can only use ONE filter
# per iter_messages call (server-side). So when multiple types are enabled,
# we use InputMessagesFilterEmpty (no server filter) and filter client-side.
# This is a minor efficiency trade-off — when only ONE type is enabled,
# we get full server-side filtering.
_FILTER_MAP = {
    "photo":     InputMessagesFilterPhotos,
    "video":     InputMessagesFilterVideo,
    "animation": InputMessagesFilterGif,
}

# A "unit" is either a single message or a whole album.
# ids  = list of message ids in the unit
# msgs = list of Telethon Message objects
Unit = Tuple[List[int], List[TelethonMessage]]


def _select_telethon_filter(enabled: set[str]):
    """
    If exactly ONE filter type is enabled, use the matching Telethon
    InputMessagesFilter for server-side filtering (more efficient).
    If MULTIPLE types are enabled, fall back to no server filter (filter client-side).

    Returns an INSTANCE of the filter class (or InputMessagesFilterEmpty()).
    """
    if len(enabled) == 1:
        only_one = next(iter(enabled))
        filter_cls = _FILTER_MAP.get(only_one)
        if filter_cls is not None:
            return filter_cls()
    return InputMessagesFilterEmpty()


async def iter_units(
    client: TelegramClient,
    order: str,
    cfg: Config,
    start_offset_id: int = 0,
) -> AsyncIterator[Unit]:
    """
    Yield (ids, messages) units from Saved Messages.

    Uses Telethon's iter_messages with:
      - reverse=True (oldest first)
      - min_id=start_offset_id (auto-resume watermark)
      - filter=InputMessagesFilter{Photos,Video,Gif} (server-side filter, single type only)

    A "unit" is either:
      - a single non-album message  → ([id], [msg])
      - a complete album             → ([id1,id2,…], [msg1,msg2,…])

    Telethon's iter_messages handles pagination, reverse, min_id, and
    server-side filtering all in one call. No manual offset_id walking,
    no PAGE_DELAY, no "load all into memory" gymnastics.
    """
    seen_in_session: set[int] = set()
    msg_filter = _select_telethon_filter(cfg.filter_types)

    # Telethon's reverse=True works natively.
    reverse = (order == "old")

    if start_offset_id > 0:
        print(f"[iter] auto-resume: starting from id > {start_offset_id} "
              f"(filter={sorted(cfg.filter_types)}, reverse={reverse})")
    else:
        print(f"[iter] starting fresh scan (filter={sorted(cfg.filter_types)}, reverse={reverse})")

    # iter_messages with reverse=True yields oldest-first, with min_id as
    # the exclusive lower bound (only messages with id > min_id).
    # Note: with reverse=True, offset_id semantics flip — but min_id still works
    # as the lower bound.
    iterator = client.iter_messages(
        "me",
        filter=msg_filter if not isinstance(msg_filter, InputMessagesFilterEmpty) else None,
        reverse=reverse,
        min_id=start_offset_id if reverse else 0,
        # When reverse=False (newest-first), use max_id to skip already-sent items.
        max_id=start_offset_id if not reverse and start_offset_id > 0 else 0,
        limit=cfg.max_scan if cfg.max_scan > 0 else None,
    )

    count_seen = 0
    async for m in iterator:
        if m is None:
            continue
        if m.id in seen_in_session:
            continue
        count_seen += 1
        if count_seen % 200 == 0:
            print(f"[iter] scanned {count_seen} messages so far…")

        # Telethon groups albums by `m.grouped_id` (same as Pyrogram's media_group_id).
        grouped_id = getattr(m, "grouped_id", None)
        if grouped_id:
            # Fetch the full album by searching for messages with the same grouped_id.
            # Note: server-side filter (InputMessagesFilterVideo) does NOT support
            # grouped_id filtering, so we use no filter here.
            try:
                album_msgs = []
                async for am in client.iter_messages("me", min_id=max(0, m.id - 50), max_id=m.id + 50, limit=100):
                    if getattr(am, "grouped_id", None) == grouped_id:
                        album_msgs.append(am)
                if not album_msgs:
                    album_msgs = [m]
            except Exception as e:
                print(f"[iter] could not fetch media group for msg_id={m.id}: {e!r}; treating as single")
                album_msgs = [m]
            ids = [x.id for x in album_msgs if x is not None]
            seen_in_session.update(ids)
            yield ids, album_msgs
        else:
            seen_in_session.add(m.id)
            yield [m.id], [m]


# ----------------------------------------------------------------------
# Forward path: bulk forward via client.forward_messages with caption stripping
# ----------------------------------------------------------------------

async def _forward_batch(
    client: TelegramClient,
    target: str,
    msgs: List[TelethonMessage],
    limiter: RateLimiter,
) -> None:
    """
    Forward a batch of up to 100 messages to the target channel.
    Strips captions (drop_media_captions=True) AND forward headers (drop_author=True).

    Telethon's forward_messages does this in a SINGLE MTProto call, vs Pyrogram's
    per-message copy_message which was 50x slower.
    """
    if not msgs:
        return

    # Limit batch to 100 (Telegram's hard limit per forwardMessages call).
    if len(msgs) > 100:
        msgs = msgs[:100]

    msg_ids = [m.id for m in msgs]

    async def send_fn():
        # drop_media_captions=True strips captions.
        # drop_author=True is REQUIRED by Telegram when drop_media_captions=True.
        # The result: destination receives a clean copy with no caption and no
        # "forwarded from" header.
        return await client.forward_messages(
            target,
            msg_ids,
            drop_author=True,
            drop_media_captions=True,
        )

    await limiter.send_with_retry(send_fn, f"forward_messages {len(msgs)} items")


# ----------------------------------------------------------------------
# Snapshot helper
# ----------------------------------------------------------------------

def _build_snapshot(tracker: ProgressTracker, cfg: Config, stop_reason: str = "") -> Snapshot:
    """Convert ProgressTracker state into a Telegram-friendly Snapshot."""
    snap = Snapshot(
        target=cfg.target,
        filter_types=set(cfg.filter_types),
        order=cfg.order,
        sweep_num=tracker.sweep_num,
        sweep_started_at=tracker.sweep_started_at,
        items_in_sweep=tracker.items_in_sweep,
        msgs_in_sweep=tracker.msgs_in_sweep,
        skipped_in_sweep=tracker.skipped_in_sweep,
        total_items_sent=tracker.total_items_sent,
        total_msgs_sent=tracker.total_msgs_sent,
        total_skipped=tracker.total_skipped,
        first_run_at=tracker.first_run_at,
        current_item_id=tracker.current_item_id,
        current_item_kind=tracker.current_item_kind,
        current_item_size=tracker.current_item_size,
        item_started_at=tracker.item_started_at,
        upload_active=tracker.upload_active,
        upload_current=tracker.upload_current,
        upload_total=tracker.upload_total,
        batch_pause_active=False,
        batch_pause_remaining=0.0,
        batch_pause_total=0.0,
        batch_num=tracker.batch_num,
        stopped=False,
        stop_reason=stop_reason,
    )
    return snap


# ----------------------------------------------------------------------
# Main sweep + loop
# ----------------------------------------------------------------------

async def _sweep(
    client: TelegramClient,
    cfg: Config,
    state: State,
    stop_watcher: StopWatcher,
    limiter: RateLimiter,
    tracker: ProgressTracker,
    tp: TelegramProgress,
    stop_reason_holder: dict,
) -> int:
    """
    One full pass over Saved Messages.
    Returns the number of NEW units sent this sweep.
    """
    sent_this_sweep = 0

    tracker.start_sweep()
    await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))

    # Auto-resume: pass the last processed message_id as the starting watermark.
    starting_offset = state.last_offset_id
    if starting_offset > 0:
        print(f"[sweep] auto-resuming from id > {starting_offset} (last processed in prior sweep)")

    # Mark the start of the FIRST burst — batch_pause() will wait until
    # batch_interval_sec has elapsed since this timestamp.
    limiter.start_burst()
    print(f"[sweep] starting burst #1 (target: {cfg.batch_size} items in {cfg.batch_interval_sec}s)")

    # Collect a batch of messages, then forward them all in one forward_messages call.
    # This is dramatically more efficient than the Pyrogram version (which copied
    # them one at a time).
    batch: List[TelethonMessage] = []
    batch_ids: List[int] = []

    async for ids, msgs in iter_units(client, cfg.order, cfg, start_offset_id=starting_offset):
        if stop_watcher.stop_requested():
            print("[forwarder] stop signal received — halting")
            break

        # Idempotency: skip if ALL ids already sent.
        if all(state.was_sent(i) for i in ids):
            for i in ids:
                state.update_offset_id(i)
            continue

        # If using server-side filter (single type only), all msgs match.
        # Otherwise (multi-type), filter client-side.
        if len(cfg.filter_types) > 1:
            matching = [m for m in msgs if is_match(m, cfg.filter_types)]
            if not matching:
                tracker.item_skipped(ids[0], f"none of {len(msgs)} items match filter")
                for i in ids:
                    state.update_offset_id(i)
                continue
            msgs = matching
            ids = [m.id for m in matching]

        # Add to the current batch.
        batch.extend(msgs)
        batch_ids.extend(ids)

        # If batch is full, forward it.
        if len(batch) >= cfg.batch_size:
            await limiter.pace()
            first_id = batch_ids[0]
            kind_label = message_kind(batch[0]) or "unknown"
            if len(batch) > 1:
                kind_label = f"album({len(batch)}×{kind_label})"
            tracker.item_start(first_id, kind_label, n_msgs=len(batch))
            await tp.update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))

            try:
                await _forward_batch(client, cfg.target, batch, limiter)
            except Exception as e:
                print(f"\n[forwarder] failed on msg_ids={batch_ids}: {e!r} — skipping (will retry next sweep)")
                batch = []
                batch_ids = []
                continue

            state.mark_sent_many(batch_ids)
            try:
                state.save()
            except Exception as e:
                print(f"\n[state] WARNING save failed: {e!r}")

            tracker.item_done()
            sent_this_sweep += 1
            await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))

            batch = []
            batch_ids = []

            # Burst pause — wait until batch_interval_sec since this burst started.
            if tracker.items_in_batch >= cfg.batch_size:
                tracker.batch_pause_start()
                await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))
                batch_total = float(limiter.batch_interval_sec)
                loop_ref = asyncio.get_running_loop()
                def _sync_tick(remaining: float):
                    tracker.batch_pause_tick(remaining)
                    snap = _build_snapshot(tracker, cfg, stop_reason_holder.get("reason", ""))
                    snap.batch_pause_active = True
                    snap.batch_pause_remaining = remaining
                    snap.batch_pause_total = batch_total
                    asyncio.run_coroutine_threadsafe(tp.update(snap), loop_ref)
                await limiter.batch_pause(on_tick=_sync_tick)
                tracker.batch_pause_end()
                limiter.start_burst()
                print(f"[sweep] starting burst #{tracker.batch_num + 1} "
                      f"(target: {cfg.batch_size} items in {cfg.batch_interval_sec}s)")
                await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))
                if stop_watcher.stop_requested():
                    break

    # Flush any remaining batch.
    if batch:
        await limiter.pace()
        first_id = batch_ids[0]
        kind_label = message_kind(batch[0]) or "unknown"
        if len(batch) > 1:
            kind_label = f"album({len(batch)}×{kind_label})"
        tracker.item_start(first_id, kind_label, n_msgs=len(batch))
        try:
            await _forward_batch(client, cfg.target, batch, limiter)
            state.mark_sent_many(batch_ids)
            try:
                state.save()
            except Exception as e:
                print(f"\n[state] WARNING save failed: {e!r}")
            tracker.item_done()
            sent_this_sweep += 1
        except Exception as e:
            print(f"\n[forwarder] failed on final batch msg_ids={batch_ids}: {e!r}")
        await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))

    tracker.end_sweep()
    print(tracker.summary())
    await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))
    return sent_this_sweep


async def run_forwarder(
    client: TelegramClient,
    cfg: Config,
    state: State,
    stop_watcher: StopWatcher,
    limiter: RateLimiter,
    tp: TelegramProgress,
    web: WebServer,
    state_sync=None,
    rescan_interval_sec: int = 60,
) -> None:
    """
    Main loop: sweep Saved Messages, then sleep rescan_interval_sec, repeat.
    Continues until stop_watcher.stop_event is set.
    """
    tracker = ProgressTracker()
    stop_reason_holder: dict = {"reason": ""}
    print(f"[forwarder] starting — target={cfg.target} filter={sorted(cfg.filter_types)} order={cfg.order}")

    # Wire web server status_provider so /status returns live JSON.
    def _web_status_provider() -> dict:
        snap = _build_snapshot(tracker, cfg, stop_reason_holder.get("reason", ""))
        return {
            "target": snap.target,
            "filter_types": sorted(snap.filter_types),
            "order": snap.order,
            "sweep_num": snap.sweep_num,
            "items_in_sweep": snap.items_in_sweep,
            "msgs_in_sweep": snap.msgs_in_sweep,
            "skipped_in_sweep": snap.skipped_in_sweep,
            "total_items_sent": snap.total_items_sent,
            "total_msgs_sent": snap.total_msgs_sent,
            "total_skipped": snap.total_skipped,
            "current_item_id": snap.current_item_id,
            "current_item_kind": snap.current_item_kind,
            "current_item_size": snap.current_item_size,
            "upload_active": snap.upload_active,
            "upload_current": snap.upload_current,
            "upload_total": snap.upload_total,
            "batch_pause_active": snap.batch_pause_active,
            "batch_pause_remaining": snap.batch_pause_remaining,
            "batch_num": snap.batch_num,
            "stopped": snap.stopped,
            "stop_reason": snap.stop_reason,
            "state_sent_ids_count": len(state.sent_ids),
            "last_offset_id": state.last_offset_id,
        }
    web.status_provider = _web_status_provider

    # Initial Telegram progress post.
    try:
        await tp.start(_build_snapshot(tracker, cfg))
    except Exception as e:
        print(f"[forwarder] TelegramProgress start failed: {e!r}; continuing without live progress")

    # Background ticker for Telegram progress updates.
    async def _ticker():
        while not stop_watcher.stop_requested():
            try:
                await tp.update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))
            except Exception as e:
                print(f"[forwarder] ticker update failed: {e!r}")
            await asyncio.sleep(max(2.0, cfg.progress_update_interval))
    ticker_task = asyncio.create_task(_ticker(), name="tg-progress-ticker")

    # Background ticker for Telegram state sync.
    async def _state_sync_ticker():
        if state_sync is None:
            return
        while not stop_watcher.stop_requested():
            await asyncio.sleep(cfg.state_sync_interval_sec)
            if stop_watcher.stop_requested():
                break
            try:
                await state_sync.sync(state.sent_ids)
            except Exception as e:
                print(f"[forwarder] state-sync tick failed: {e!r}")
    sync_task = asyncio.create_task(_state_sync_ticker(), name="tg-state-sync-ticker")

    try:
        while not stop_watcher.stop_requested():
            try:
                await _sweep(client, cfg, state, stop_watcher, limiter, tracker, tp, stop_reason_holder)
            except Exception as e:
                print(f"[forwarder] sweep crashed: {e!r}")

            if stop_watcher.stop_requested():
                break

            print(f"[forwarder] re-scanning Saved Messages in {rescan_interval_sec}s…")
            end = time.time() + rescan_interval_sec
            while time.time() < end and not stop_watcher.stop_requested():
                await asyncio.sleep(2)
    finally:
        ticker_task.cancel()
        sync_task.cancel()
        for t in (ticker_task, sync_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        stop_reason_holder["reason"] = stop_reason_holder.get("reason") or "Stopped by user / shutdown"
        try:
            await tp.stop(_build_snapshot(tracker, cfg, stop_reason_holder["reason"]))
        except Exception as e:
            print(f"[forwarder] final tp.stop failed: {e!r}")

    print("[forwarder] exited main loop — goodbye.")
