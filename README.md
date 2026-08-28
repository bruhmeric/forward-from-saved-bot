# Telegram Saved Messages Bulk Forwarder

A Python **web service** that pulls media out of your **Saved Messages** and forwards it in 50-item batches to a designated channel/group — with no captions, conservative rate limiting, and a **web dashboard** at `/` for live progress + a `POST /stop` endpoint for halting it remotely.

Built to run on **Render's free tier** (web service + StringSession auth). Also works locally.

> **Note on Render free tier:** background workers are paid-only, so this bot runs as a web service that binds to `$PORT` and serves a tiny HTTP status page. The bot still runs continuously; the HTTP server is just there to satisfy Render's platform health check AND to give you a clean web UI for monitoring + control. By default, NO messages are posted to your Saved Messages — the web UI is the only progress surface.

---

## What it does

1. Connects to Telegram using **your personal user account** (via Pyrogram `StringSession`) — required because Saved Messages is a user-only feature, not accessible to bot tokens.
2. Iterates Saved Messages (oldest-first by default, configurable to newest-first).
3. Skips anything that isn't a photo / video / animation (GIF) per your `--filter` choice.
4. **Keeps albums together** — multi-photo posts are re-sent as a single `send_media_group` call.
5. **Strips captions** — every item is forwarded with `caption=""`, so the destination is media-only.
6. **Conservative rate limiting** — ~2.5s between messages, then a 2-3 minute pause after every 50.
7. **Persists sent IDs** to `state.json` locally. (On Render free tier, this is wiped on redeploy by default — set `USE_TELEGRAM_STATE_SYNC=1` if you want it mirrored to Saved Messages as a document, or upgrade to a paid Disk.)
8. **Stops on `POST /stop`** — hit the HTTP endpoint, click the Stop button on `/`, press Ctrl+C, or use Render's Suspend button. All halt the bot gracefully after the current item.
9. After a full sweep, sleeps 5 min and re-scans — so newly saved items get picked up without a restart.
10. **Live upload progress in web UI** — every item shows: sweep number, item number, message id, kind (photo/video/animation/album), per-upload byte progress (when a real upload is needed), cumulative totals, and items/min rate. The `/` page auto-refreshes every 10 seconds.
11. **JSON status API** at `GET /status` for external monitoring (UptimeRobot, custom dashboards, etc.).
12. **(Optional) Telegram-side live progress** — set `TELEGRAM_PROGRESS=1` to ALSO mirror progress to your Saved Messages. Default OFF.

---

## Project layout

```
tg-bulk-forwarder/
├── main.py                  # CLI entrypoint + orchestration
├── config.py                # Env vars + CLI flag merging
├── forwarder.py             # Core loop: sweep → filter → send → mark_sent
├── filters.py               # Photo / Video / Animation matcher
├── progress.py              # Console live progress bars (sweep, item, upload, batch pause)
├── telegram_progress.py     # Telegram-side live progress message (in-place edits)
├── telegram_state_sync.py   # Mirror state.json to Saved Messages (free-tier persistence hack)
├── web_server.py            # Tiny asyncio HTTP server on $PORT (/, /health, /status, /stop)
├── rate_limiter.py          # Pacing + FloodWait auto-retry
├── stop_signal.py           # Background thread polling Saved Messages for /stop & /status
├── state.py                 # state.json persistence (atomic writes)
├── session_setup.py         # One-time local script → prints SESSION_STRING
├── render.yaml              # Render WEB SERVICE config (free tier)
├── Dockerfile               # Docker image (alternative to native Python runtime)
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
✅ SESSION_STRING generated successfully!
======================================================================

COPY ONLY THE LINE BELOW (no quotes, no 'SESSION_STRING=' prefix):

AQB1dHg6...long_string_here...XYZ

======================================================================

HOW TO USE IT:

  • In Render → Environment → add a new var:
      Key:   SESSION_STRING
      Value: <paste the line above, exactly as-is>

  • In local .env:
      SESSION_STRING=<paste the line above>

DO NOT:
  ✗ include 'SESSION_STRING=' in the Value field (Render adds that)
  ✗ wrap it in quotes
  ✗ add spaces at the start/end

Length: 370 chars · Format: Pyrogram v2 StringSession
```

### Critical: how to paste into Render's env var

When you create the env var on Render, you have two fields:
- **Key**: `SESSION_STRING`
- **Value**: paste the LONG STRING ONLY — **NOT** `SESSION_STRING=ABC123`, just `ABC123`

If you accidentally paste `SESSION_STRING=ABC123` into the Value field, the bot will fail with `struct.error: unpack requires a buffer of 271 bytes`. The bot has built-in auto-cleaning that strips this prefix, but if you still see that error, double-check the raw Value field on Render.

If you're still hitting that error after re-pasting, see the "Troubleshooting" section below.

---

## 3) Run locally (optional — for testing before deploying)

```bash
cp .env.example .env
# Edit .env: fill in API_ID, API_HASH, SESSION_STRING, TARGET
```

Then:

```bash
# Default: photo+video+animation, oldest first, conservative pacing
python main.py

# Override on the CLI
python main.py --target=@my_channel --filter=photo --order=old
python main.py --target=-1001234567890 --filter=video,animation
```

To stop locally: either press `Ctrl+C`, or send `/stop` to your Saved Messages from any Telegram client.

---

## 4) Deploy to Render (free tier — WEB SERVICE)

> Render's free tier does **not** support background workers. This project runs as a **web service** instead — it binds to `$PORT` and serves a tiny HTTP server (just `/health`, `/status`, `/`, `/stop`) alongside the forwarding loop. The bot itself still runs continuously; the HTTP server is just there to satisfy Render's platform health check.

### Option A: Connect a GitHub repo (recommended)

1. Push this folder to a GitHub repo.
2. In Render Dashboard → **New** → **Web Service**.
3. Connect your GitHub repo.
4. Render should auto-detect `render.yaml`. If not, set:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`
   - **Plan**: Free
5. Under **Environment**, set these required vars:
   - `API_ID` = (from step 1)
   - `API_HASH` = (from step 1)
   - `SESSION_STRING` = (from step 2)
   - `TARGET` = `@your_channel` or `-1001234567890`
6. (Optional) Set the other env vars from `.env.example` to override defaults.
7. Click **Create Web Service**.

Render auto-sets `PORT` (usually 10000). The bot binds to `0.0.0.0:$PORT` within ~5 seconds of boot and serves `/health` → `OK` for the platform health check.

### Option B: Docker image

Use the included `Dockerfile`:
- Render → New → **Web Service** → **Docker** runtime → point at the repo.
- Same env vars as Option A.

### Why no persistent disk?

Render's free tier doesn't include persistent disks — `state.json` would be wiped on every deploy or sleep cycle. **The bot works around this by mirroring state.json as a document to your Saved Messages** (every 60s by default). On startup, the bot scans the latest `[BULK-FORWARDER-STATE]` document and restores its resume memory.

If you later upgrade to a paid Render plan with a Disk:
1. Mount a 1GB disk at `/data`.
2. Set `STATE_FILE=/data/state.json` and `PROGRESS_MESSAGE_ID_FILE=/data/progress_msg_id.txt`.
3. Set `USE_TELEGRAM_STATE_SYNC=0` (no longer needed).

---

## 5) Render free-tier limitations & workarounds

### The 15-minute sleep problem

Render free web services **sleep after 15 minutes of no inbound HTTP traffic**. While sleeping, the bot is fully paused — no media gets forwarded.

**Fix:** set up an external uptime monitor that pings your service every ~10 minutes:

- [UptimeRobot](https://uptimerobot.com/) (free, 50 monitors)
- [cron-job.org](https://cron-job.org/) (free)
- [Better Stack](https://betterstack.com/) (free tier)

Point it at `https://your-service.onrender.com/health` with HTTP 200 expected. The bot will be kept awake indefinitely.

### The 750 instance-hours/month limit

Render free tier includes 750 instance-hours/month (~31 days of always-on). With the uptime monitor workaround above, you'll likely hit this limit before the month ends. The bot will pause until the next billing cycle starts, then resume automatically.

**Practical guidance:**
- 750 hours ≈ enough to forward ~3,000-5,000 photos per month at conservative pacing.
- If you need more, upgrade to Render's Starter plan ($7/month, always-on).

### State persistence across redeploys

The bot mirrors `state.json` to your Saved Messages as a JSON document every 60s. On startup, the latest mirror is restored. This means:

- ✅ A redeploy keeps your resume memory.
- ✅ A crash keeps your resume memory.
- ✅ A Render sleep/wake cycle keeps your resume memory.
- ⚠️ If you manually delete the state document from Saved Messages, the bot will start fresh next time.
- ⚠️ If you change `TARGET`, the bot will ignore the old state doc (target mismatch).

### Cold start delays

When Render wakes the service, it takes ~30-60s to boot Python, install deps (if cached), connect to Telegram, and start forwarding. The HTTP `/health` endpoint is up within ~5s, so Render won't kill the service, but you'll see a brief delay before media starts flowing.

---

## Stopping the bot

Three ways to halt (web UI is the primary control surface):

1. **HTTP**: `curl -X POST https://your-service.onrender.com/stop` — or click the **Stop bot** button on the `/` status page.
2. **Ctrl+C** when running locally.
3. **Render's Suspend button** sends `SIGTERM`, which the bot handles the same way.

In all cases, the bot finishes the current item, saves state (and mirrors it to Saved Messages if `USE_TELEGRAM_STATE_SYNC=1`), then exits cleanly.

> **Optional:** if you set `TELEGRAM_PROGRESS=1`, you can ALSO send `/stop`, `/halt`, or `/kill` to your Saved Messages from any Telegram client. By default this is OFF — the web UI is the only control surface.

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
| `ORDER` | `old` | `new` (newest first) or `old` (oldest first — DEFAULT). Note: `old` requires loading ALL Saved Messages into memory before forwarding starts (Pyrogram 2.0.106 doesn't support `reverse=True`). For 10,000+ saved items, consider `ORDER=new` to stream. |
| `BATCH_SIZE` | `50` | Items per batch before long pause (1-50) |
| `PER_MESSAGE_DELAY` | `2.5` | Seconds between individual sends |
| `BATCH_PAUSE_MIN` | `120` | Min seconds pause after each batch |
| `BATCH_PAUSE_MAX` | `180` | Max seconds pause after each batch |
| `STOP_POLL_INTERVAL` | `5` | Seconds between Saved Messages polls for `/stop` |
| `STATE_FILE` | `./state.json` | Path to local sent-IDs state file (ephemeral on free tier) |
| `PORT` | (Render auto-sets) | HTTP server port — Render injects this automatically |
| `TELEGRAM_PROGRESS` | `0` | `1`/`on` to ALSO post live progress to PROGRESS_CHAT (default OFF — web UI is primary) |
| `PROGRESS_CHAT` | `me` | Where live progress + `/stop` + `/status` commands are watched (only used if `TELEGRAM_PROGRESS=1`) |
| `PROGRESS_UPDATE_INTERVAL` | `5.0` | Min seconds between Telegram edits (≥2) |
| `PROGRESS_MESSAGE_ID_FILE` | (next to STATE_FILE) | Persists live message_id so restart edits the same message |
| `USE_TELEGRAM_STATE_SYNC` | `0` | Mirror state.json to PROGRESS_CHAT (default OFF — your "already sent" list resets on Render redeploy unless you upgrade to a paid Disk) |
| `STATE_SYNC_INTERVAL_SEC` | `60` | How often to push state.json to Telegram (≥30s) |

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

## Telegram-side live progress message (optional, OFF by default)

> **Default behavior**: the web UI at `/` is the only progress surface. The bot does NOT post anything to your Saved Messages. This section describes the OPTIONAL Telegram-side mirror you can enable with `TELEGRAM_PROGRESS=1`.

If you want a Telegram-side mirror of progress IN ADDITION to the web UI, set `TELEGRAM_PROGRESS=1`. The bot will post ONE message to your Saved Messages (or any chat you configure) and edit it in place every ~5 seconds with the current sweep #, items sent, current item, upload %, batch-pause countdown, and cumulative totals. You'll also be able to send `/stop` and `/status` to that chat as Telegram messages.

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

## HTTP status page & API

When deployed, the bot serves a tiny HTTP server on Render's `$PORT`. Routes:

| Method | Path | Returns | Use case |
|---|---|---|---|
| `GET` | `/` | HTML status dashboard (auto-refreshes every 10s) | Human-friendly view; click "Stop bot" button |
| `GET` | `/health` | `OK` | Render platform health check (must respond within 60s of boot) |
| `GET` | `/status` | JSON snapshot of current progress | Programmatic monitoring, external dashboards |
| `POST` | `/stop` | `stop signal received…` | Halt the bot via curl/script |
| `GET` | `/favicon.ico` | 204 No Content | Browser favicon suppression |

Example:

```bash
# Check bot status from CLI
curl https://your-service.onrender.com/status | jq .

# Halt the bot from CLI
curl -X POST https://your-service.onrender.com/stop
```

The `/` HTML page auto-refreshes every 10 seconds — leave it open in a browser tab for a live dashboard.

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

### `struct.error: unpack requires a buffer of 271 bytes`

This is the #1 most common Render deployment error. It means the `SESSION_STRING` env var value is malformed. Causes & fixes:

| Cause | Fix |
|---|---|
| **Value includes `SESSION_STRING=` prefix** | Render's env var has two fields: **Key** and **Value**. Put `SESSION_STRING` in Key, and ONLY the long string (no `SESSION_STRING=` prefix) in Value. |
| **Value is wrapped in quotes** | Render env vars don't need quotes. Remove any `"` or `'` you may have added. |
| **Value is truncated** | Pyrogram v2 sessions are ~370 chars. If yours is shorter, your terminal may have truncated it during copy-paste. Re-run `session_setup.py` and copy more carefully — try `pbcopy`/`xclip` if available. |
| **Value has whitespace/newlines** | Some terminals add a trailing newline. The bot auto-strips these, but check the raw Value field on Render. |
| **Generated with wrong Pyrogram version** | If you ran `session_setup.py` with a different Pyrogram version than `requirements.txt` specifies (pyrogram 2.0.106), the session format won't match. Re-run it inside a fresh `pip install -r requirements.txt` env. |
| **Session was revoked** | If you logged out from Telegram's "Active Sessions" page or revoked it some other way, regenerate it. |

The bot now prints a friendly diagnostic on Render when this happens, including the session length and a 30-char preview so you can compare it with what `session_setup.py` printed locally.

### Other common issues

| Symptom | Likely cause / fix |
|---|---|
| `SESSION_STRING` rejected on Render | Run `session_setup.py` again locally — sessions can expire. |
| `FloodWait` constantly | Lower `BATCH_SIZE` or raise `PER_MESSAGE_DELAY`. Telegram may also be throttling your account globally. |
| `PeerIdInvalid` for target | Make sure you've opened the target channel once from your account, or use the `-100…` numeric id. |
| Bot is running but nothing forwards | Check the `FILTER` — items in Saved Messages that are stickers, voice notes, or documents don't match any filter. |
| State resets on Render redeploy | Should not happen — the bot mirrors state to Saved Messages every 60s. If it does, check `USE_TELEGRAM_STATE_SYNC=1` is set and `PROGRESS_CHAT=me`. |
| Stop command not detected | The watcher only reacts to `/stop` sent **after** the bot started. Older historical `/stop` messages in Saved Messages are ignored. |
| Album comes through split into single photos | Telegram's album grouping can be lost if any item in the album was forwarded/deleted before fetch. The bot will retry the next sweep. |
| Service keeps sleeping on free tier | Render free web services sleep after 15 min of no inbound HTTP. Set up UptimeRobot to ping `https://your-service.onrender.com/health` every 10 min. |

---

## License

MIT — do whatever you want. No warranty. Be a good Telegram citizen and don't abuse rate limits.
