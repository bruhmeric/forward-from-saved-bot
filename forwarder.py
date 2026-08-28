"""
Core forwarding logic.

Iterates the user's Saved Messages, yields each "unit" (single message OR
a whole album), filters by media type, and forwards to the target channel
with captions stripped. Idempotent via State.sent_ids. Respects stop signal.

Two send paths:
  1. PRIMARY: `copy_message` / `copy_media_group` — server-side copy, no
     actual file transfer. Fast, no bandwidth used. Caption overridden to "".
  2. FALLBACK: if copy_* fails with MEDIA_GROUPED_INVALID / generic error,
     re-send via `send_photo` / `send_video` / `send_media_group` with the
     file downloaded first. This path triggers Pyrogram's `progress` callback
     so the ProgressTracker can show real upload percentage.
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, List, Tuple

from pyrogram import Client
from pyrogram.types import Message

from config import Config
from filters import is_match, message_kind
from progress import ProgressTracker, make_upload_callback
from rate_limiter import RateLimiter
from state import State
from stop_signal import StopWatcher
from telegram_progress import Snapshot, TelegramProgress
from telegram_state_sync import TelegramStateSync
from web_server import WebServer


Unit = Tuple[List[int], List[Message]]  # ([msg_id, ...], [Message, ...])


async def iter_units(
    client: Client,
    order: str,
    max_scan: int = 5000,
    page_delay: float = 0.4,
    start_offset_id: int = 0,
) -> AsyncIterator[Unit]:
    """
    Yield (ids, messages) units from Saved Messages.

    A "unit" is either:
      - a single non-album message  → ([id], [msg])
      - a complete album            → ([id1,id2,…], [msg1,msg2,…])

    Order handling:
      - "new" (newest first): iterate directly via get_chat_history()
      - "old" (oldest first): paginate backward (offset_id) collecting pages,
        then reverse in memory and yield oldest-first.

    Pyrogram 2.0.106's get_chat_history() does NOT support reverse=True,
    so for oldest-first we must materialize the history in RAM.

    Args:
      max_scan: cap on total messages to collect (0 = unlimited). Default 5000.
                Caps memory + scan time on huge Saved Messages.
      page_delay: seconds to sleep between get_chat_history pages.
                  Telegram rate-limits GetHistory to ~30 calls/30s. Default 0.4s
                  keeps us under the limit and avoids FloodWait penalties.
      start_offset_id: AUTO-RESUME watermark — only collect messages with id
                       STRICTLY GREATER than this value. Pass state.last_offset_id
                       to skip already-processed items on subsequent sweeps.
                       0 = collect from the beginning.
    """
    seen_in_session: set[int] = set()

    if order == "old":
        # Collect Saved Messages into a list, paginating backward.
        # Auto-resume: skip messages with id <= start_offset_id (already processed).
        all_msgs: list = []
        offset_id = 0  # Pyrogram's offset for pagination (walks backward from newest)
        page_num = 0
        scan_started_at = time.time()
        # 0 means unlimited; otherwise cap at max_scan.
        effective_cap = max_scan if max_scan > 0 else float("inf")
        hit_cap = False
        hit_existing_watermark = start_offset_id > 0

        if hit_existing_watermark:
            print(f"[iter] auto-resume: skipping messages with id ≤ {start_offset_id} "
                  f"(already processed in prior sweeps)")

        while len(all_msgs) < effective_cap:
            page_num += 1
            page_msgs: list = []
            kwargs = {"limit": 100}
            if offset_id > 0:
                kwargs["offset_id"] = offset_id
            try:
                async for m in client.get_chat_history("me", **kwargs):
                    if m is not None:
                        # Auto-resume: skip already-processed items.
                        if hit_existing_watermark and m.id <= start_offset_id:
                            # We've walked back to the watermark — stop scanning.
                            # (Messages come in newest-first order, so once we hit
                            # an id <= watermark, everything older is also already done.)
                            break
                        page_msgs.append(m)
                        if len(all_msgs) + len(page_msgs) >= effective_cap:
                            break  # don't over-collect beyond the cap
            except Exception as e:
                print(f"[iter] get_chat_history page #{page_num} failed (offset_id={offset_id}): {e!r}")
                break

            if not page_msgs:
                break  # reached the end OR hit the watermark

            # Trim to the cap if needed.
            remaining_capacity = effective_cap - len(all_msgs)
            if remaining_capacity < len(page_msgs):
                page_msgs = page_msgs[:remaining_capacity]
                hit_cap = True

            all_msgs.extend(page_msgs)
            # offset_id = the LOWEST id in this page; next page returns messages with id < that
            lowest_id = min(m.id for m in page_msgs)
            if lowest_id == offset_id:
                break  # no progress — bail to avoid infinite loop
            offset_id = lowest_id

            # If we hit the watermark mid-page, stop scanning further back.
            if hit_existing_watermark and lowest_id <= start_offset_id:
                print(f"[iter] reached auto-resume watermark (id={start_offset_id}); "
                      f"collected {len(all_msgs)} new messages")
                break

            # Progress log every 5 pages (or every page if cap is close).
            elapsed = time.time() - scan_started_at
            rate = len(all_msgs) / elapsed if elapsed > 0 else 0
            if page_num % 5 == 0 or hit_cap:
                cap_str = f"/{int(effective_cap)}" if effective_cap != float("inf") else ""
                print(f"[iter] collected {len(all_msgs)}{cap_str} messages "
                      f"({page_num} pages, {rate:.0f} msgs/sec, {elapsed:.1f}s elapsed)")
            if hit_cap:
                print(f"[iter] hit MAX_SCAN cap of {max_scan}; stopping collection")
                break

            # Throttle to avoid Telegram's GetHistory FloodWait (~30 calls per 30s).
            if page_delay > 0:
                await asyncio.sleep(page_delay)

        if not all_msgs:
            print(f"[iter] no new messages to process (watermark={start_offset_id}); "
                  f"nothing to forward this sweep")
            return

        print(f"[iter] collected {len(all_msgs)} new messages in {page_num} pages "
              f"({time.time() - scan_started_at:.1f}s); reversing for oldest-first")
        all_msgs.reverse()  # now oldest-first

        for m in all_msgs:
            if m is None or m.id in seen_in_session:
                continue
            if m.media_group_id:
                try:
                    album = await client.get_media_group("me", m.id)
                except Exception as e:
                    print(f"[iter] could not fetch media group for msg_id={m.id}: {e!r}; treating as single")
                    album = [m]
                ids = [x.id for x in album if x is not None]
                seen_in_session.update(ids)
                yield ids, album
            else:
                seen_in_session.add(m.id)
                yield [m.id], [m]
        return

    # Default path: newest-first (Pyrogram's natural order)
    count = 0
    async for m in client.get_chat_history("me"):
        if m is None or m.id in seen_in_session:
            continue
        # Auto-resume: skip already-processed items (newest-first mode).
        if start_offset_id > 0 and m.id <= start_offset_id:
            # In newest-first order, once we hit the watermark, everything older is done.
            print(f"[iter] newest-first: reached watermark id={start_offset_id}; stopping")
            return

        if m.media_group_id:
            try:
                album = await client.get_media_group("me", m.id)
            except Exception as e:
                print(f"[iter] could not fetch media group for msg_id={m.id}: {e!r}; treating as single")
                album = [m]
            ids = [x.id for x in album if x is not None]
            seen_in_session.update(ids)
            yield ids, album
        else:
            seen_in_session.add(m.id)
            yield [m.id], [m]

        count += 1
        # Also respect max_scan in newest-first mode (treat 0 as unlimited).
        if max_scan > 0 and count >= max_scan:
            print(f"[iter] newest-first path hit MAX_SCAN cap of {max_scan}; stopping")
            return


# ----------------------------------------------------------------------
# PRIMARY path: server-side copy (no upload)
# ----------------------------------------------------------------------

async def _copy_single(
    client: Client, target: str, msg: Message, limiter: RateLimiter
) -> None:
    async def send_fn():
        return await client.copy_message(target, "me", msg.id, caption="")
    await limiter.send_with_retry(send_fn, f"copy_message {msg.id}")


async def _copy_album(
    client: Client, target: str, album: List[Message], limiter: RateLimiter
) -> None:
    first = album[0]
    captions = [""] * len(album)
    async def send_fn():
        return await client.copy_media_group(target, "me", first.id, captions=captions)
    await limiter.send_with_retry(send_fn, f"copy_media_group {first.id} (x{len(album)})")


# ----------------------------------------------------------------------
# FALLBACK path: actual upload with progress callback
# ----------------------------------------------------------------------

async def _upload_single(
    client: Client,
    target: str,
    msg: Message,
    limiter: RateLimiter,
    tracker: ProgressTracker,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """
    Download the media from Saved Messages, then re-upload it to the target
    with an empty caption. The `progress` callback updates the tracker with
    upload percentage.
    """
    kind = message_kind(msg)
    if kind is None:
        raise RuntimeError(f"cannot upload non-media msg_id={msg.id}")

    cb = make_upload_callback(tracker, loop)

    async def send_fn():
        # Download to a temp file in memory-like buffer.
        # Pyrogram: use client.download_media for the file, then send_* it.
        # We use in_memory=True so we don't touch the disk.
        path = await client.download_media(msg, in_memory=True)
        if path is None:
            raise RuntimeError(f"download returned None for msg_id={msg.id}")

        if kind == "photo":
            await client.send_photo(target, path, caption="", progress=cb)
        elif kind == "video":
            await client.send_video(target, path, caption="", progress=cb)
        elif kind == "animation":
            await client.send_animation(target, path, caption="", progress=cb)
        else:
            raise RuntimeError(f"unsupported kind for upload: {kind}")

    await limiter.send_with_retry(send_fn, f"upload {kind} {msg.id}")


async def _upload_album(
    client: Client,
    target: str,
    album: List[Message],
    limiter: RateLimiter,
    tracker: ProgressTracker,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """
    Download every item in the album, then send as a media group with empty
    captions. Upload progress is the SUM across all items.
    """
    cb = make_upload_callback(tracker, loop)

    async def send_fn():
        # Pre-download all items.
        paths = []
        for m in album:
            p = await client.download_media(m, in_memory=True)
            if p is None:
                raise RuntimeError(f"download returned None for msg_id={m.id}")
            paths.append(p)

        # Build media_group list.
        from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaAnimation
        media = []
        for m, p in zip(album, paths):
            kind = message_kind(m)
            if kind == "photo":
                media.append(InputMediaPhoto(media=p, caption=""))
            elif kind == "video":
                media.append(InputMediaVideo(media=p, caption=""))
            elif kind == "animation":
                media.append(InputMediaAnimation(media=p, caption=""))
            else:
                raise RuntimeError(f"unsupported kind in album: {kind}")

        await client.send_media_group(target, media_group=media)
        # send_media_group doesn't expose a progress param; we'd need to wrap
        # each item. For now, leave tracker.upload_active False for albums.

    await limiter.send_with_retry(send_fn, f"upload_album {album[0].id} (x{len(album)})")


# ----------------------------------------------------------------------
# Dispatch: try copy first, fall back to upload on failure
# ----------------------------------------------------------------------

async def _dispatch_single(
    client: Client, target: str, msg: Message,
    limiter: RateLimiter, tracker: ProgressTracker, loop: asyncio.AbstractEventLoop,
) -> None:
    try:
        await _copy_single(client, target, msg, limiter)
    except Exception as e:
        print(f"[forwarder] copy failed for msg_id={msg.id}: {e!r} — falling back to upload")
        await _upload_single(client, target, msg, limiter, tracker, loop)


async def _dispatch_album(
    client: Client, target: str, album: List[Message],
    limiter: RateLimiter, tracker: ProgressTracker, loop: asyncio.AbstractEventLoop,
) -> None:
    try:
        await _copy_album(client, target, album, limiter)
    except Exception as e:
        print(f"[forwarder] copy_album failed for msg_id={album[0].id}: {e!r} — falling back to upload")
        await _upload_album(client, target, album, limiter, tracker, loop)


# ----------------------------------------------------------------------
# Snapshot helper: build a Snapshot from the in-memory tracker state
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
    client: Client,
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
    loop = asyncio.get_running_loop()
    sent_this_sweep = 0

    tracker.start_sweep()
    # Initial Telegram progress update for this sweep.
    await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))

    # Auto-resume: pass the last processed message_id as the starting watermark.
    # iter_units will skip any messages with id ≤ this value (already processed).
    starting_offset = state.last_offset_id
    if starting_offset > 0:
        print(f"[sweep] auto-resuming from id > {starting_offset} (last processed in prior sweep)")

    # Mark the start of the FIRST burst — batch_pause() will wait until
    # batch_interval_sec has elapsed since this timestamp.
    limiter.start_burst()
    print(f"[sweep] starting burst #1 (target: {cfg.batch_size} items in {cfg.batch_interval_sec}s)")

    async for ids, msgs in iter_units(client, cfg.order,
                                      max_scan=cfg.max_scan,
                                      page_delay=cfg.page_delay,
                                      start_offset_id=starting_offset):
        if stop_watcher.stop_requested():
            print("[forwarder] stop signal received — halting")
            break

        # Idempotency: skip if ALL ids already sent.
        if all(state.was_sent(i) for i in ids):
            # Update offset watermark even for skipped items so we don't re-visit.
            for i in ids:
                state.update_offset_id(i)
            continue

        # Filter: at least one message in the unit must match the filter set.
        # If a unit has mixed media (e.g., a video+photo album), we keep it.
        matching = [m for m in msgs if is_match(m, cfg.filter_types)]
        if not matching:
            tracker.item_skipped(ids[0], f"none of {len(msgs)} items match filter")
            # Still advance the offset watermark so we don't re-scan this item.
            for i in ids:
                state.update_offset_id(i)
            continue

        # Determine kind label for progress display.
        if len(msgs) > 1:
            kinds = sorted({message_kind(m) for m in matching if message_kind(m)})
            kind_label = "+".join(kinds)
        else:
            kind_label = message_kind(matching[0]) or "unknown"

        # Pre-send pacing (within-burst delay).
        await limiter.pace()

        # Mark item start in the tracker.
        first_id = ids[0]
        tracker.item_start(first_id, kind_label, n_msgs=len(matching))
        await tp.update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))

        # Send — album vs single, with upload fallback.
        try:
            if len(msgs) > 1:
                await _dispatch_album(client, cfg.target, msgs, limiter, tracker, loop)
            else:
                await _dispatch_single(client, cfg.target, msgs[0], limiter, tracker, loop)
        except Exception as e:
            print(f"\n[forwarder] failed on msg_ids={ids}: {e!r} — skipping (will retry next sweep)")
            continue

        # Mark as sent & persist immediately.
        state.mark_sent_many(ids)
        try:
            state.save()
        except Exception as e:
            print(f"\n[state] WARNING save failed: {e!r}")

        tracker.item_done()
        sent_this_sweep += 1
        await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))

        # Burst pause — wait until batch_interval_sec since this burst started.
        if tracker.items_in_batch >= cfg.batch_size:
            tracker.batch_pause_start()
            await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))
            # Wrap on_tick so the tracker AND telegram progress both update.
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
            # Start the NEXT burst timer immediately after the pause ends.
            limiter.start_burst()
            print(f"[sweep] starting burst #{tracker.batch_num + 1} "
                  f"(target: {cfg.batch_size} items in {cfg.batch_interval_sec}s)")
            await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))
            if stop_watcher.stop_requested():
                break

    tracker.end_sweep()
    print(tracker.summary())
    await tp.force_update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))
    return sent_this_sweep


async def run_forwarder(
    client: Client,
    cfg: Config,
    state: State,
    stop_watcher: StopWatcher,
    limiter: RateLimiter,
    tp: TelegramProgress,
    web: WebServer,
    state_sync: TelegramStateSync | None = None,
    rescan_interval_sec: int = 300,
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
        # Convert Snapshot dataclass to dict for JSON serialization.
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

    # Background task: tick TelegramProgress every interval so upload progress
    # and idle times still update even when no other event triggers.
    async def _ticker():
        while not stop_watcher.stop_requested():
            try:
                await tp.update(_build_snapshot(tracker, cfg, stop_reason_holder.get("reason", "")))
            except Exception as e:
                print(f"[forwarder] ticker update failed: {e!r}")
            await asyncio.sleep(max(2.0, cfg.progress_update_interval))
    ticker_task = asyncio.create_task(_ticker(), name="tg-progress-ticker")

    # Background task: periodic state-sync to Telegram so a free-tier redeploy
    # doesn't lose resume memory.
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
        # Final update with stopped banner.
        stop_reason_holder["reason"] = stop_reason_holder.get("reason") or "Stopped by user / shutdown"
        try:
            await tp.stop(_build_snapshot(tracker, cfg, stop_reason_holder["reason"]))
        except Exception as e:
            print(f"[forwarder] final tp.stop failed: {e!r}")

    print("[forwarder] exited main loop — goodbye.")
