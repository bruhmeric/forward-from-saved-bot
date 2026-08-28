"""
Main entrypoint.

Usage (local):
    python main.py --target=@my_channel --filter=photo,video --order=new

Usage (Render free tier — WEB SERVICE, not background worker):
    Set env vars (API_ID, API_HASH, SESSION_STRING, TARGET, FILTER, ORDER, …).
    Render auto-sets $PORT; the bot binds to 0.0.0.0:$PORT and serves a tiny
    HTTP server (/, /health, /status, /stop) alongside the forwarding loop.
    State is mirrored to your Saved Messages so redeploys/sleeps don't lose
    resume memory.

Stop:
    - POST /stop       (HTTP)
    - Send "/stop"     to your Saved Messages (Telegram)
    - Ctrl+C           (local)
    - Render 'Suspend' button (SIGTERM)
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
from telegram_progress import Snapshot, TelegramProgress
from telegram_state_sync import TelegramStateSync
from web_server import WebServer


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
    p.add_argument("--web-port", type=int,
                   help="HTTP server port (default $PORT or 10000).")
    p.add_argument("--web-host", default="0.0.0.0",
                   help="HTTP server bind host (default 0.0.0.0).")
    p.add_argument("--use-telegram-state-sync", choices=["1","0","on","off"],
                   help="Mirror state.json to a Telegram chat so free-tier redeploys don't lose progress. Default on.")
    p.add_argument("--state-sync-interval", type=int,
                   help="How often to push state.json to Telegram (default 60s).")
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
    if args.web_port:       o["web_port"]              = args.web_port
    if args.web_host:       o["web_host"]              = args.web_host
    if args.use_telegram_state_sync: o["use_telegram_state_sync"] = args.use_telegram_state_sync
    if args.state_sync_interval: o["state_sync_interval_sec"]   = str(args.state_sync_interval)
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
    print(f"  web_server     : http://{cfg.web_host}:{cfg.web_port}")
    print(f"  state_sync     : {'on' if cfg.use_telegram_state_sync else 'off'} → {cfg.progress_chat!r} (every {cfg.state_sync_interval_sec}s)")

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

    # Telegram state-sync (mirrors state.json to Saved Messages so redeploys don't lose progress).
    state_sync: TelegramStateSync | None = None
    if cfg.use_telegram_state_sync:
        state_sync = TelegramStateSync(
            client=client, chat=cfg.progress_chat, target=cfg.target,
        )

    # Tiny HTTP server — Render free-tier requires binding to $PORT.
    # Constructed now, started after Pyrogram connects.
    web = WebServer(
        port=cfg.web_port,
        host=cfg.web_host,
        # status_provider is wired after the tracker is created, in run_forwarder.
        status_provider=lambda: {},
        on_stop=stop_watcher.request_stop,
    )

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

    # Bootstrap state from Telegram (if enabled) — overrides local state.json
    # if a Telegram state doc exists (it's the more durable copy on free tier).
    if state_sync is not None:
        try:
            tg_sent_ids = await state_sync.bootstrap()
            if tg_sent_ids:
                # Merge: Telegram wins on conflict.
                state.sent_ids = tg_sent_ids
                state.save()
                print(f"[main] restored {len(tg_sent_ids)} sent_ids from Telegram state doc")
        except Exception as e:
            print(f"[main] Telegram state-sync bootstrap failed: {e!r}; continuing with local state")

    # Start the web server FIRST so Render's health check passes within 60s.
    try:
        await web.start()
    except Exception as e:
        print(f"[main] WARNING: web server failed to start: {e!r}")
        print("[main] (Render will probably kill the service — ensure $PORT is set and not already in use)")

    # Start the /stop watcher.
    stop_watcher.start_background()

    try:
        await run_forwarder(client, cfg, state, stop_watcher, limiter, tp, web,
                            state_sync=state_sync,
                            rescan_interval_sec=rescan_interval)
    finally:
        try:
            state.save()
        except Exception as e:
            print(f"[main] state save on exit failed: {e!r}")
        # Final state sync to Telegram so the next startup has fresh data.
        if state_sync is not None:
            try:
                await state_sync.sync(state.sent_ids)
            except Exception as e:
                print(f"[main] final Telegram state-sync failed: {e!r}")
        try:
            await web.stop()
        except Exception:
            pass
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
