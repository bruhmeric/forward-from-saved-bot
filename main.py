"""
Main entrypoint.

Usage (local):
    python main.py --target=@my_channel --filter=photo,video --order=new

Usage (Render):
    Set env vars (API_ID, API_HASH, SESSION_STRING, TARGET, FILTER, ORDER, …)
    and the worker will pick them up automatically.

Stop:
    Send "/stop" (or "/halt" or "/kill") to your own Saved Messages from any
    Telegram client. The watcher polls Saved Messages every 5s and halts the
    bot gracefully after the current item.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from typing import Optional

from pyrogram import Client

from config import Config, from_env
from forwarder import run_forwarder
from rate_limiter import RateLimiter
from state import State
from stop_signal import StopWatcher
from telegram_progress import TelegramProgress


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Telegram Saved Messages bulk forwarder (user session via Pyrogram)."
    )
    p.add_argument("--target",     help="Destination channel/group (@username or -100… id).")
    p.add_argument("--filter",     help="Comma-separated: photo,video,animation (default: all).")
    p.add_argument("--order",      choices=["new", "old"], help="new=recent first, old=oldest first.")
    p.add_argument("--batch-size", type=int, help="Messages per batch before long pause (1-50).")
    p.add_argument("--per-message-delay", type=float, help="Seconds between individual sends.")
    p.add_argument("--batch-pause-min",  type=int, help="Min pause seconds after each batch.")
    p.add_argument("--batch-pause-max",  type=int, help="Max pause seconds after each batch.")
    p.add_argument("--api-id",     help="Telegram API_ID (or set API_ID env).")
    p.add_argument("--api-hash",   help="Telegram API_HASH (or set API_HASH env).")
    p.add_argument("--session-string", help="Pyrogram StringSession (or set SESSION_STRING env).")
    p.add_argument("--state-file", help="Path to state.json (default ./state.json).")
    p.add_argument("--telegram-progress", choices=["1","0","on","off"],
                   help="Post live progress to a Telegram chat (1=on, 0=off). Default on.")
    p.add_argument("--progress-chat", help="Where to post live progress (default 'me' = Saved Messages).")
    p.add_argument("--progress-update-interval", type=float,
                   help="Min seconds between Telegram progress edits (default 5).")
    p.add_argument("--rescan-interval", type=int, default=300,
                   help="Seconds between full Saved Messages rescans (default 300).")
    return p.parse_args()


def _cli_overrides(args: argparse.Namespace) -> dict:
    """Build a dict of CLI overrides, only including provided flags."""
    o: dict = {}
    if args.target:          o["target"]              = args.target
    if args.filter:          o["filter"]              = args.filter
    if args.order:           o["order"]               = args.order
    if args.batch_size:      o["batch_size"]          = args.batch_size
    if args.per_message_delay: o["per_message_delay"] = args.per_message_delay
    if args.batch_pause_min: o["batch_pause_min"]     = args.batch_pause_min
    if args.batch_pause_max: o["batch_pause_max"]      = args.batch_pause_max
    if args.api_id:          o["api_id"]              = args.api_id
    if args.api_hash:        o["api_hash"]             = args.api_hash
    if args.session_string:  o["session_string"]       = args.session_string
    if args.state_file:      o["state_file"]           = args.state_file
    if args.telegram_progress: o["telegram_progress"]   = args.telegram_progress
    if args.progress_chat:  o["progress_chat"]         = args.progress_chat
    if args.progress_update_interval: o["progress_update_interval"] = str(args.progress_update_interval)
    return o


async def amain(cfg: Config, rescan_interval: int) -> int:
    print("[main] config loaded")
    print(f"  target         : {cfg.target}")
    print(f"  filter         : {sorted(cfg.filter_types)}")
    print(f"  order          : {cfg.order}")
    print(f"  batch_size     : {cfg.batch_size}")
    print(f"  per_message    : {cfg.per_message_delay}s")
    print(f"  batch_pause    : {cfg.batch_pause_min}-{cfg.batch_pause_max}s")
    print(f"  state_file     : {cfg.state_file}")
    print(f"  tg_progress    : {'on' if cfg.telegram_progress else 'off'} → {cfg.progress_chat!r} (every {cfg.progress_update_interval}s)")

    # Build Pyrogram client from StringSession — no file I/O, perfect for Render.
    client = Client(
        name="bulk_forwarder",  # ignored when session_string provided
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        session_string=cfg.session_string,
        no_updates=True,  # we don't need realtime updates; saves bandwidth
        workdir="/tmp",    # avoid Render's read-only FS for transient stuff
    )

    # State (sent IDs) — persisted file.
    state = State(path=cfg.state_file, target=cfg.target)

    # Rate limiter.
    limiter = RateLimiter(
        per_message_delay=cfg.per_message_delay,
        batch_pause_min=cfg.batch_pause_min,
        batch_pause_max=cfg.batch_pause_max,
    )

    # Stop watcher (background thread with own event loop). Uses the same
    # control chat as TelegramProgress so /stop and /status land in one place.
    stop_watcher = StopWatcher(
        client=client,
        poll_interval=cfg.stop_poll_interval,
        control_chat=cfg.progress_chat,
    )

    # Telegram live-progress reporter.
    tp = TelegramProgress(
        client=client,
        chat=cfg.progress_chat,
        update_interval=cfg.progress_update_interval,
        message_id_file=cfg.progress_message_id_file,
    )
    if not cfg.telegram_progress:
        tp.disable()

    # Ctrl+C handler — also flips the stop event.
    def _sigint(*_):
        print("\n[main] Ctrl+C received — requesting graceful stop after current item…")
        stop_watcher.request_stop()
    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)  # Render sends SIGTERM on shutdown

    print("[main] starting Pyrogram client…")
    await client.start()
    me = await client.get_me()
    print(f"[main] connected as {me.first_name} (@{me.username or '—'}) id={me.id}")

    # Start the /stop watcher.
    stop_watcher.start_background()

    try:
        await run_forwarder(client, cfg, state, stop_watcher, limiter, tp,
                            rescan_interval_sec=rescan_interval)
    finally:
        try:
            state.save()
        except Exception as e:
            print(f"[main] state save on exit failed: {e!r}")
        try:
            await client.stop()
        except Exception:
            pass
        print("[main] done.")

    return 0


def main() -> int:
    args = parse_args()
    cfg = from_env(_cli_overrides(args))
    try:
        return asyncio.run(amain(cfg, rescan_interval=args.rescan_interval))
    except KeyboardInterrupt:
        print("\n[main] interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
