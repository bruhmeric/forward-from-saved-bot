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

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

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
        description="Telegram Saved Messages bulk forwarder (Telethon user session)."
    )
    p.add_argument("--target",     help="Destination channel/group (@username or -100… id).")
    p.add_argument("--filter",     help="Comma-separated: photo,video,animation (default: all).")
    p.add_argument("--order",      choices=["new", "old"], help="new=recent first, old=oldest first.")
    p.add_argument("--batch-size", type=int, help="Messages per batch before long pause (1-50).")
    p.add_argument("--per-message-delay", type=float, help="Seconds between individual sends.")
    p.add_argument("--batch-pause-min",  type=int, help="Min pause seconds after each batch.")
    p.add_argument("--batch-pause-max",  type=int, help="Max pause seconds after each batch.")
    p.add_argument("--max-scan", type=int,
                   help="Cap on total Saved Messages scanned per sweep (default 5000; 0 = unlimited).")
    p.add_argument("--page-delay", type=float,
                   help="Delay (seconds) between get_chat_history pages (default 0.4; prevents FloodWait).")
    p.add_argument("--api-id",     help="Telegram API_ID (or set API_ID env).")
    p.add_argument("--api-hash",   help="Telegram API_HASH (or set API_HASH env).")
    p.add_argument("--session-string", help="Telethon StringSession (or set SESSION_STRING env).")
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
    p.add_argument("--rescan-interval", type=int, default=60,
                   help="Seconds between full Saved Messages rescans to pick up new items (default 60).")
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
    if args.max_scan:       o["max_scan"]              = args.max_scan
    if args.page_delay:     o["page_delay"]            = args.page_delay
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


def _validate_session_string(s: str) -> None:
    """
    Pre-flight check on the SESSION_STRING before handing it to Pyrogram.

    Pyrogram v2 StringSessions are ~370 chars, base64url-ish (chars from
    A–Z a–z 0–9 _ -). We don't decode it fully — just sanity-check the
    length and character set so we can give a friendly error instead of
    Pyrogram's cryptic 'struct.error: unpack requires a buffer of 271 bytes'.
    """
    if not s:
        raise SystemExit(
            "❌ SESSION_STRING is empty. Run `python session_setup.py` locally "
            "to generate one, then paste the printed string into your env vars."
        )

    # Pyrogram v2 session strings are typically ~370 chars. v1 is ~290 chars.
    # Anything < 200 is definitely truncated or wrong format.
    if len(s) < 200:
        raise SystemExit(
            f"❌ SESSION_STRING is too short ({len(s)} chars; expected ~370 for "
            "Pyrogram v2). It may have been truncated during copy-paste, or generated "
            "with a different Pyrogram version. Re-run `python session_setup.py`."
        )

    # If it's still got the 'SESSION_STRING=' prefix, our cleaning didn't catch it
    # (e.g. value is 'SESSION_STRING=SESSION_STRING=ABC...'). Tell the user.
    if "SESSION_STRING" in s[:30]:
        print(f"⚠️  WARNING: SESSION_STRING value starts with 'SESSION_STRING=' prefix.")
        print(f"    First 30 chars: {s[:30]!r}")
        print(f"    Render's env var VALUE should contain ONLY the session string,")
        print(f"    not 'SESSION_STRING=...'. Please re-paste it without the prefix.")
        raise SystemExit(1)

    # Check character set. Pyrogram v2 uses base64url alphabet (A-Z a-z 0-9 _ -).
    # Be lenient — allow = for padding just in case.
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-=")
    bad_chars = set(s) - allowed
    if bad_chars:
        sample = "".join(sorted(bad_chars))[:10]
        raise SystemExit(
            f"❌ SESSION_STRING contains invalid characters: {sample!r}\n"
            "    Pyrogram v2 session strings only use A-Z a-z 0-9 _ - = \n"
            "    This usually means the string was corrupted during copy-paste "
            "(e.g. line breaks inserted, HTML-escaped, or copy-pasted through a "
            "rich-text editor). Re-copy from the terminal output of session_setup.py."
        )

    # All good — print preview for debugging.
    preview = s[:30] + "…" + s[-10:]
    print(f"[main] SESSION_STRING looks valid ({len(s)} chars: {preview})")


async def amain(cfg: Config, rescan_interval: int) -> int:
    print("[main] config loaded")
    print(f"  target         : {cfg.target}")
    print(f"  filter         : {sorted(cfg.filter_types)}")
    print(f"  order          : {cfg.order}")
    print(f"  batch_size     : {cfg.batch_size} msgs/burst (Telegram's sustained limit is ~30/min)")
    print(f"  per_message    : {cfg.per_message_delay}s (within-burst delay)")
    items_per_min = (cfg.batch_size * 60) / cfg.batch_interval_sec
    print(f"  batch_interval : {cfg.batch_interval_sec}s (between burst starts → ~{items_per_min:.0f} items/min)")
    print(f"  max_scan       : {cfg.max_scan if cfg.max_scan > 0 else 'unlimited'}")
    print(f"  page_delay     : {cfg.page_delay}s")
    print(f"  state_file     : {cfg.state_file}")
    print(f"  tg_progress    : {'on' if cfg.telegram_progress else 'off'} → {cfg.progress_chat!r} (every {cfg.progress_update_interval}s)")
    print(f"  web_server     : http://{cfg.web_host}:{cfg.web_port}")
    print(f"  state_sync     : {'on' if cfg.use_telegram_state_sync else 'off'} → {cfg.progress_chat!r} (every {cfg.state_sync_interval_sec}s)")

    # Build Telethon client from StringSession — no file I/O, perfect for Render.
    client = TelegramClient(
        StringSession(cfg.session_string),
        cfg.api_id,
        cfg.api_hash,
    )

    # State (sent IDs) — persisted file.
    state = State(path=cfg.state_file, target=cfg.target)

    # Rate limiter — burst pacing model:
    #   - PER_MESSAGE_DELAY between sends within a burst (default 0.5s → 50 items in ~25s)
    #   - BATCH_INTERVAL_SEC between burst STARTS (default 60s → ~50 items/min throughput)
    limiter = RateLimiter(
        per_message_delay=cfg.per_message_delay,
        batch_interval_sec=cfg.batch_interval_sec,
    )

    # Stop watcher — runs as an asyncio task on Pyrogram's event loop.
    # Only polls the control chat for /stop & /status if TELEGRAM_PROGRESS=1
    # (or USE_TELEGRAM_STATE_SYNC=1, which also implies Saved Messages is in use).
    # When both are OFF, the watcher just owns the asyncio.Event and the web UI
    # POST /stop is the only control surface.
    stop_watcher = StopWatcher(
        client=client,
        poll_interval=cfg.stop_poll_interval,
        control_chat=cfg.progress_chat,
        poll_control_chat=cfg.telegram_progress or cfg.use_telegram_state_sync,
    )
    # Hold a reference to the running event loop so the SIGTERM/SIGINT handler
    # (which runs in a different thread) can flip the stop_event safely.
    main_loop = asyncio.get_running_loop()

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
    def _reset_watermark():
        """Reset the auto-resume watermark so next sweep re-scans from id=1."""
        old = state.last_offset_id
        state.reset_offset()
        try:
            state.save()
            print(f"[main] auto-resume watermark reset (was {old}, now 0) via HTTP /reset")
        except Exception as e:
            print(f"[main] state save after reset failed: {e!r}")

    web = WebServer(
        port=cfg.web_port,
        host=cfg.web_host,
        # status_provider is wired after the tracker is created, in run_forwarder.
        status_provider=lambda: {},
        on_stop=stop_watcher.request_stop,
        on_reset=_reset_watermark,
    )

    # Ctrl+C / SIGTERM handler — runs in a separate thread (signal handlers
    # always do), so we MUST use call_soon_threadsafe to mutate asyncio state.
    def _sigint(*_):
        print("\n[main] Ctrl+C/SIGTERM received — requesting graceful stop after current item…")
        main_loop.call_soon_threadsafe(stop_watcher.request_stop)
    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)  # Render sends SIGTERM on shutdown

    # Validate the session string format BEFORE attempting to start Telethon.
    # This catches the most common deployment mistake (pasting the value with
    # the 'SESSION_STRING=' prefix included, or wrapping in quotes, or copying
    # only part of it) and gives a friendly error instead of a cryptic
    # 'struct.error: unpack requires a buffer of 271 bytes'.
    _validate_session_string(cfg.session_string)

    print("[main] starting Telethon client…")
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Session not authorized — the SESSION_STRING is invalid or has been revoked.")
    except Exception as e:
        msg = str(e)
        print()
        print("=" * 70)
        print("❌ FAILED TO START TELETHON CLIENT")
        print("=" * 70)
        print(f"Error: {type(e).__name__}: {msg}")
        print()
        print("This is almost always a SESSION_STRING problem. Common causes:")
        print()
        print("  1. SESSION_STRING was generated with a DIFFERENT library")
        print("     (Telethon vs Pyrogram). Re-run `python session_setup.py` locally")
        print("     — it now generates a TELETHON StringSession (not Pyrogram).")
        print()
        print("  2. SESSION_STRING was copy-pasted with extra characters (quotes,")
        print("     'SESSION_STRING=' prefix, trailing whitespace). We auto-strip")
        print("     common ones, but check the raw env var value on Render.")
        print()
        print("  3. SESSION_STRING was truncated.")
        print(f"     Yours is {len(cfg.session_string)} chars.")
        print()
        print("  4. The session was revoked (you logged out, or revoked it from")
        print("     Telegram's 'Active Sessions' page). Re-generate it.")
        print()
        print(f"SESSION_STRING preview (first 30 chars): {cfg.session_string[:30]}…")
        print("=" * 70)
        raise SystemExit(1)

    me = await client.get_me()
    first_name = getattr(me, "first_name", None) or "?"
    username = getattr(me, "username", None)
    uid = getattr(me, "id", "?")
    print(f"[main] connected as {first_name} (@{username or '—'}) id={uid}")

    # CRITICAL: Resolve the target peer BEFORE the forwarder starts sending.
    # StringSession stores NO entity cache, so the first time we use a numeric
    # id like -1004343021949, Telethon has no access_hash for it and fails
    # with "Cannot find any entity corresponding to ...".
    #
    # Fix: call get_dialogs() once to populate the session cache with all the
    # user's chats (channels, groups, DMs). After this, get_entity(numeric_id)
    # works because the access_hash is cached in memory.
    #
    # If the target is a @username, get_entity() works without this warmup —
    # but we do it anyway to also enable replying to /stop and /status commands
    # sent to Saved Messages from various Telegram clients.
    print(f"[main] warming up session cache (fetching dialogs)…")
    try:
        dialog_count = 0
        async for _ in client.iter_dialogs():
            dialog_count += 1
        print(f"[main] ✓ session cache warmed up: {dialog_count} dialogs indexed")
    except Exception as e:
        print(f"[main] WARNING: dialog warmup failed: {e!r}")
        print("[main] (continuing — target resolution may fail for numeric ids)")

    print(f"[main] resolving target peer {cfg.target!r}…")
    try:
        target_entity = await client.get_entity(cfg.target)
        title = (getattr(target_entity, "title", None)
                 or getattr(target_entity, "first_name", None)
                 or getattr(target_entity, "username", None)
                 or "?")
        cid = getattr(target_entity, "id", "?")
        print(f"[main] ✓ target resolved: {title!r} (id={cid})")
    except Exception as e:
        print()
        print("=" * 70)
        print("❌ FAILED TO RESOLVE TARGET — bot will not be able to send anything")
        print("=" * 70)
        print(f"Error: {type(e).__name__}: {e}")
        print()
        print(f"TARGET is currently set to: {cfg.target!r}")
        print()
        print("Common causes:")
        print()
        print("  1. Target is a private channel/group and your account isn't a member.")
        print("     Join the channel from your Telegram app first, then redeploy.")
        print()
        print("  2. Target is a @username that doesn't exist or is misspelled.")
        print()
        print("  3. Target is a numeric id (-1001234567890) but you've never")
        print("     interacted with that chat from this account. Open it from")
        print("     your Telegram app once, then redeploy.")
        print()
        print("  4. For supergroups/channels, the id format MUST be -100<id>")
        print("     (e.g., -1001234567890). Without the -100 prefix, Telegram")
        print("     treats it as a user id and resolution fails.")
        print()
        print("  5. ⚠️  If you're sure the id is correct and you're a member,")
        print("     try using the channel's @username instead — some private")
        print("     channels don't expose their numeric id to the API even")
        print("     when you're an admin.")
        print()
        raise SystemExit(1)

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

    # Start the /stop watcher on Pyrogram's event loop.
    stop_watcher.start()

    try:
        await run_forwarder(client, cfg, state, stop_watcher, limiter, tp, web,
                            state_sync=state_sync,
                            rescan_interval_sec=rescan_interval)
    finally:
        # Cancel the /stop watcher task so it doesn't leak.
        try:
            await stop_watcher.stop()
        except Exception as e:
            print(f"[main] stop_watcher shutdown failed: {e!r}")
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
            await client.disconnect()
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
