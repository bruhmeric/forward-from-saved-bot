# Telegram Saved Messages Bulk Forwarder

A Python background worker that pulls media out of your **Saved Messages** and forwards it in 50-item batches to a designated channel/group — with no captions, conservative rate limiting, and a remote `/stop` command so you can halt it from any Telegram client.

Built to run as a **Render Background Worker** (stateless host + StringSession auth), but works locally too.

---

## What it does

1. Connects to Telegram using **your personal user account** (via Pyrogram `StringSession`) — required because Saved Messages is a user-only feature, not accessible to bot tokens.
2. Iterates Saved Messages (newest-first by default, configurable to oldest-first).
3. Skips anything that isn't a photo / video / animation (GIF) per your `--filter` choice.
4. **Keeps albums together** — multi-photo posts are re-sent as a single `send_media_group` call.
5. **Strips captions** — every item is forwarded with `caption=""`, so the destination is media-only.
6. **Conservative rate limiting** — ~2.5s between messages, then a 2-3 minute pause after every 50.
7. **Persists sent IDs** to `state.json`, so a crash / redeploy / restart will resume from where it left off without re-sending.
8. **Stops on `/stop`** — send `/stop`, `/halt`, or `/kill` to your own Saved Messages from any Telegram client, and the watcher halts the bot after the current item.
9. After a full sweep, sleeps 5 min and re-scans — so newly saved items get picked up without a restart.
10. **Live upload progress** — every item shows: sweep number, item number, message id, kind (photo/video/animation/album), per-upload byte progress (when a real upload is needed), cumulative totals, and items/min rate. Batch pauses show a live countdown bar.
11. **Telegram-side live progress message** — the bot posts ONE message to your Saved Messages (or any chat you configure) and keeps editing it in place with the current sweep #, items sent, current item, upload %, batch-pause countdown, and cumulative totals. Send `/status` to the same chat for an on-demand snapshot.

---

## Project layout

```
tg-bulk-forwarder/
├── main.py              # CLI entrypoint + orchestration
├── config.py            # Env vars + CLI flag merging
├── forwarder.py         # Core loop: sweep → filter → send → mark_sent
├── filters.py           # Photo / Video / Animation matcher
├── progress.py          # Console live progress bars (sweep, item, upload, batch pause)
├── telegram_progress.py # Telegram-side live progress message (in-place edits)
├── rate_limiter.py      # Pacing + FloodWait auto-retry
├── stop_signal.py       # Background thread polling Saved Messages for /stop & /status
├── state.py             # state.json persistence (atomic writes)
├── session_setup.py     # One-time local script → prints SESSION_STRING
├── render.yaml          # Render Background Worker config
├── Dockerfile           # Docker image (alternative to native Python runtime)
├── requirements.txt
├── .env.example
└── README.md
```

---

## 1) Get your Telegram API credentials

You need an **API ID** and **API HASH** from Telegram (not a bot token).

1. Visit https://my.telegram.org → **API Development Tools**.
2. Fill the form (any app name, any short name, platform can be "Desktop").
3. Note down `api_id` (numeric) and `api_hash` (long alphanumeric string).

> ⚠️ These credentials are tied to **your personal Telegram account**. Don't commit them to git.

---

## 2) Generate the StringSession (run locally once)

This step **must run on a machine where you can receive the Telegram login code** — Render's headless environment won't work because it can't prompt you for the SMS/app code.

```bash
cd tg-bulk-forwarder
pip install -r requirements.txt
python session_setup.py
```

You'll be prompted for:
- `API_ID` (paste from step 1)
- `API_HASH` (paste from step 1)
- `Phone` (with country code, e.g. `+447700900000`)
- The Telegram login code (sent to your Telegram app — sometimes SMS)
- Your 2FA password if you have one enabled

The script will print something like:

```
======================================================================
SESSION_STRING=AB3Def9hBz_zZZ...long_string_here...XYZ
======================================================================
```

**Copy the entire `SESSION_STRING=...` value** (just the part after `=`) — you'll paste it into Render (or your `.env`).

---

## 3) Run locally (optional — for testing before deploying)

```bash
cp .env.example .env
# Edit .env: fill in API_ID, API_HASH, SESSION_STRING, TARGET
```

Then:

```bash
# Default: photo+video+animation, newest first, conservative pacing
python main.py

# Override on the CLI
python main.py --target=@my_channel --filter=photo --order=old
python main.py --target=-1001234567890 --filter=video,animation
```

To stop locally: either press `Ctrl+C`, or send `/stop` to your Saved Messages from any Telegram client.

---

## 4) Deploy to Render

### Option A: Connect a GitHub repo (recommended)

1. Push this folder to a GitHub repo.
2. In Render Dashboard → **New** → **Background Worker**.
3. Connect your GitHub repo.
4. Render should auto-detect `render.yaml`. If not, set:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`
5. Under **Environment**, set these required vars:
   - `API_ID` = (from step 1)
   - `API_HASH` = (from step 1)
   - `SESSION_STRING` = (from step 2)
   - `TARGET` = `@your_channel` or `-1001234567890`
6. (Optional) Set the other env vars from `.env.example` to override defaults.
7. Click **Create Background Worker**.

### Option B: Docker image

Use the included `Dockerfile`:
- Render → New → **Background Worker** → **Docker** runtime → point at the repo.
- Same env vars as Option A.

### Add a persistent disk (so `state.json` survives redeploys)

Without a disk, Render's filesystem is ephemeral — `state.json` is wiped on every deploy, so resume won't work across redeploys.

1. In your Render service → **Disks** → **Add Disk**.
2. Mount path: `/data`
3. Size: 1 GB (smallest available — more than enough).
4. Set `STATE_FILE=/data/state.json` in Environment.

---

## Stopping the bot

From **any** Telegram client (mobile, desktop, web):

1. Open your own **Saved Messages** chat.
2. Send `/stop` (or `/halt` or `/kill`).
3. The background watcher polls Saved Messages every 5 seconds and halts the bot gracefully after the current item finishes.

The bot will reply "🛑 Stop signal received…" in your Saved Messages to confirm.

You can also press `Ctrl+C` if running locally, or use Render's "Suspend" button — both send `SIGTERM`, which the bot handles the same way.

---

## Configuration reference

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `API_ID` | — | Telegram API ID (required) |
| `API_HASH` | — | Telegram API hash (required) |
| `SESSION_STRING` | — | Pyrogram StringSession from `session_setup.py` (required) |
| `TARGET` | — | Destination `@username` or `-100…` id (required) |
| `FILTER` | `photo,video,animation` | Comma-separated subset of {photo, video, animation} |
| `ORDER` | `new` | `new` (newest first) or `old` (oldest first) |
| `BATCH_SIZE` | `50` | Items per batch before long pause (1-50) |
| `PER_MESSAGE_DELAY` | `2.5` | Seconds between individual sends |
| `BATCH_PAUSE_MIN` | `120` | Min seconds pause after each batch |
| `BATCH_PAUSE_MAX` | `180` | Max seconds pause after each batch |
| `STOP_POLL_INTERVAL` | `5` | Seconds between Saved Messages polls for `/stop` |
| `STATE_FILE` | `./state.json` | Path to sent-IDs state file |

### CLI overrides

Every env var has a matching CLI flag — use `python main.py --help` to see them all. CLI flags override env vars.

```
--target, --filter, --order, --batch-size, --per-message-delay,
--batch-pause-min, --batch-pause-max, --api-id, --api-hash,
--session-string, --state-file, --rescan-interval
```

---

## Live progress output

Every line in the log is rendered in place (using ANSI carriage return) so you get a continuously-updating single line per active operation. Example of what you'll see:

```
=== Sweep #1 started ===
→ sweep#1 item#0 msg_id=4839 [photo] — cumulative 0 items / 0 msgs (0.0/min)
✓ sweep#1 item#1 msg_id=4839 [photo] — cumulative 1 items / 1 msgs (12.4/min) (4.8s)

→ sweep#1 item#2 msg_id=4840 [album(3×video)] — cumulative 1 items / 1 msgs (12.4/min)
✓ sweep#1 item#3 msg_id=4840 [album(3×video)] — cumulative 2 items / 4 msgs (15.8/min) (11.2s)

→ sweep#1 item#4 msg_id=4841 [photo] — cumulative 2 items / 4 msgs (15.8/min)
↑ sweep#1 item#4 msg_id=4841 [photo] upload [██████░░░░░░]  50.0% — cumulative 2 items / 4 msgs (15.8/min)
↑ sweep#1 item#4 msg_id=4841 [photo] upload [████████████] 100.0% — cumulative 2 items / 4 msgs (15.8/min)
✓ sweep#1 item#5 msg_id=4841 [photo] upload [████████████] 100.0% — cumulative 3 items / 5 msgs (16.1/min) (7.4s)

  ↪ skip msg_id=4842: none of 1 items match filter

  ⏸  Batch #1 complete — pausing…
  ⏸  Batch pause [██░░░░░░░░░░] 96s remaining
  ⏸  Batch pause [██████░░░░░░] 48s remaining
  ⏸  Batch pause [████████████] 0s remaining
  ▶  Resuming after batch pause.

=== Sweep #1 done: 50 items / 73 msgs in 642.3s (4.7 items/min, 12 skipped) ===

--- Cumulative ---
  sweeps:        1
  items sent:    50
  messages sent: 73
  skipped:       12
  runtime:       642s (4.7 items/min avg)
```

**Legend:**

| Glyph | Meaning |
|---|---|
| `→` | item send just started |
| `↑` | real upload in progress (bytes flowing from your machine to Telegram — happens only when `copy_message` fails and we fall back to `send_photo` / `send_video` / `send_animation`) |
| `✓` | item send complete (with elapsed time) |
| `↪` | item skipped (filter miss, already sent, etc.) |
| `⏸` | batch pause countdown |
| `▶` | resuming after batch pause |

**Why most items don't show upload progress:**

When the bot uses `copy_message` / `copy_media_group`, Telegram performs a **server-side copy** — your client only sends a small reference, no file transfer happens. That's fast and bandwidth-efficient. Upload progress bars only appear in the **fallback path** when the copy fails (e.g., the source message was deleted, the media type doesn't support copy, or Telegram returned an obscure error) and the bot re-sends the file by downloading it to memory and uploading it fresh.

To **force** the upload path (so you always see progress bars), edit `forwarder.py` and replace `_dispatch_single` / `_dispatch_album` with the `_upload_*` variants directly. Not recommended — it's much slower and uses your bandwidth.

**Reading the live bar in Render logs:**

Render's log viewer renders ANSI cursor codes correctly. If you tail logs via `render logs --tail`, you'll see the bar updating in place. If you're using a non-ANSI terminal (rare), set `NO_COLOR=1` to get plain line-by-line output instead.

---

## Telegram-side live progress message

In addition to the console progress bars, the bot posts a **single live-updating message** to a Telegram chat (defaults to your Saved Messages) and edits it in place every few seconds. This lets you monitor the bot from any Telegram client without reading Render logs.

### What it shows

```
▶️ Bulk Forwarder — Live Progress
Status: RUNNING
━━━━━━━━━━━━━━━━━━━━━━━━
📍 Target: @my_channel
📍 Filter: photo,video  ·  Order: new

🔄 Sweep #3 · 4m12s elapsed
   📦 23 items · 41 msgs · 5 skipped
   ⚡ 5.4 items/min

📊 Cumulative
   📦 127 items · 234 msgs · 12 skipped
   ⏱ 31m runtime · 4.1 items/min avg

🔄 Current item
   msg_id=4839 · [photo]
   ⏱ 4.2s elapsed
   ↑ Upload [████████░░] 67.5% (4.8/7.1 MB)

━━━━━━━━━━━━━━━━━━━━━━━━
🕒 Last update: 14:23:11
💬 Send /stop to halt · /status for snapshot
```

During a batch pause, the message shows a live countdown:

```
⏸ Bulk Forwarder — Live Progress
Status: PAUSED
…

⏸ Batch #1 pause
   [██████░░░░] 48s remaining (of 120s)
```

When the bot stops (via `/stop`, Ctrl+C, or Render SIGTERM), the final edit shows:

```
🛑 Bulk Forwarder — Live Progress
Status: STOPPED · Stopped by user / shutdown
…
```

### Commands

| Command | Action |
|---|---|
| `/stop` (or `/halt`, `/kill`) | Halt the bot gracefully after the current item |
| `/status` | Reply with a fresh one-shot snapshot (compact text) |

Send these as a normal message to the control chat (`PROGRESS_CHAT`, defaults to your Saved Messages). The watcher polls every `STOP_POLL_INTERVAL` seconds (default 5).

### Configuration

| Env var | Default | Description |
|---|---|---|
| `TELEGRAM_PROGRESS` | `1` | `1`/`on` to enable, `0`/`off` to disable. When off, only console logs are produced. |
| `PROGRESS_CHAT` | `me` | Chat where the live message lives + where `/stop` & `/status` are watched. `me` = Saved Messages. |
| `PROGRESS_UPDATE_INTERVAL` | `5.0` | Min seconds between Telegram edits. Must be ≥ 2 to avoid FloodWait. |
| `PROGRESS_MESSAGE_ID_FILE` | (next to `STATE_FILE`) | Persists the live message's id so a restart edits the same message instead of posting a new one. |

CLI equivalents: `--telegram-progress on|off`, `--progress-chat=@mycontrol`, `--progress-update-interval=10`.

### Restart behavior

The bot persists the live message's id to `progress_msg_id.txt` (next to `state.json` — so on Render, mount the same disk and both files survive redeploys). On restart:

1. The bot tries to edit the existing message.
2. If the message was deleted or is in a different chat, it posts a fresh one and updates the file.

This means a Render redeploy won't spam your Saved Messages with duplicate progress messages.

### Disabling

If you don't want Telegram-side progress at all (e.g., you find it noisy), set `TELEGRAM_PROGRESS=0` or pass `--telegram-progress off`. You'll still see console progress bars in Render logs.

---

## How caption stripping works

For **single media**: Pyrogram's `client.copy_message(target, source, msg_id, caption="")` re-sends the same media object with a fresh (empty) caption. No re-download/upload — Telegram server references the original file.

For **albums**: `client.copy_media_group(target, source, msg_id, captions=["", "", ...])` does the same for the whole media group, preserving album grouping.

If a particular Telegram update type doesn't support the `caption=""` override (rare edge cases), the unit will be logged and skipped — it'll be retried on the next sweep.

---

## Safety & limits

- **Conservative pacing**: ~2.5s/message + 2-3 min pause after every 50 ⇒ ~250 messages/hour. Telegram's user-account limit is ~30 msg/sec burst / ~200 msg/min sustained, so we're nowhere near it.
- **FloodWait auto-retry**: if Telegram says "wait N seconds", we sleep N+2s and retry up to 3 times.
- **Atomic state writes**: `state.json` is written via temp-file + rename, so a crash mid-write never corrupts it.
- **Per-item error isolation**: a failure on one item doesn't kill the run — it's logged and skipped, retried next sweep.
- **No re-sending on restart**: state.json tracks all sent Saved Messages IDs. Resume is idempotent.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `SESSION_STRING` rejected on Render | Run `session_setup.py` again locally — sessions can expire. |
| `FloodWait` constantly | Lower `BATCH_SIZE` or raise `PER_MESSAGE_DELAY`. Telegram may also be throttling your account globally. |
| `PeerIdInvalid` for target | Make sure you've opened the target channel once from your account, or use the `-100…` numeric id. |
| Bot is running but nothing forwards | Check the `FILTER` — items in Saved Messages that are stickers, voice notes, or documents don't match any filter. |
| State resets on Render redeploy | Mount a persistent Disk at `/data` and set `STATE_FILE=/data/state.json`. |
| Stop command not detected | The watcher only reacts to `/stop` sent **after** the bot started. Older historical `/stop` messages in Saved Messages are ignored. |
| Album comes through split into single photos | Telegram's album grouping can be lost if any item in the album was forwarded/deleted before fetch. The bot will retry the next sweep. |

---

## License

MIT — do whatever you want. No warranty. Be a good Telegram citizen and don't abuse rate limits.
