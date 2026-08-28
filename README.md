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
6. **Burst pacing** — sends 50 items back-to-back (0.5s between each = ~25s burst), then waits until 60s have elapsed since the burst started (~35s idle). Result: steady **~50 items per minute** throughput, which is what Telegram tolerates for sustained bulk sending to a single chat.
7. **Persists sent IDs** to `state.json` locally. (On Render free tier, this is wiped on redeploy by default — set `USE_TELEGRAM_STATE_SYNC=1` if you want it mirrored to Saved Messages as a document, or upgrade to a paid Disk.)
8. **Stops on `POST /stop`** — hit the HTTP endpoint, click the Stop button on `/`, press Ctrl+C, or use Render's Suspend button. All halt the bot gracefully after the current item.
9. After a full sweep, sleeps 60s and re-scans — so newly saved items are picked up within ~1 minute of you saving them. The auto-resume watermark means only NEW items are fetched on each re-scan (not the entire history).
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
| `ORDER` | `old` | `new` (newest first) or `old` (oldest first — DEFAULT). Note: `old` loads ALL Saved Messages into memory before forwarding (Pyrogram 2.0.106 doesn't support `reverse=True`). For 10,000+ saved items, raise `MAX_SCAN` or use `ORDER=new` to stream. |
| `MAX_SCAN` | `5000` | Cap on total Saved Messages collected per sweep (only relevant when `ORDER=old`). `0` = unlimited. Caps memory at ~2 KB × MAX_SCAN. Lower this if you're hitting Render free-tier RAM limits. |
| `PAGE_DELAY` | `0.4` | Seconds to sleep between `get_chat_history` pagination calls. Telegram rate-limits `GetHistory` to ~30 calls per 30s; without this delay, you'll see `Waiting for 24 seconds before continuing (required by "messages.GetHistory")` spam in logs. |
| `BATCH_SIZE` | `50` | Items per burst (1-50) |
| `PER_MESSAGE_DELAY` | `0.5` | Seconds between sends WITHIN a burst. 50 × 0.5 = 25s burst. Lower = faster burst but higher FloodWait risk (Telegram's per-chat limit is ~20 msg/sec). |
| `BATCH_INTERVAL_SEC` | `60` | Seconds between burst STARTS. Default 60 → ~50 items/min sustained throughput. If a burst overruns this (e.g. due to FloodWait), no extra pause is added. Must be ≥ 10. |
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

## Auto-resume (skip already-scanned items)

Every sweep, the bot normally re-scans Saved Messages from the beginning (or from the newest end, depending on `ORDER`). This is wasteful when you have thousands of items — you'd spend minutes re-paginating through items you've already processed.

**Auto-resume** solves this by tracking the **highest Saved Messages `message_id` the bot has processed** (`last_offset_id` in `state.json`). On each new sweep, the bot:

1. Reads `last_offset_id` from `state.json`
2. Asks Telegram for messages with `id > last_offset_id` only
3. Updates the watermark after every item (sent OR skipped)
4. Persists it to `state.json` immediately so a crash mid-sweep doesn't lose progress

This means: if you've already forwarded items 1–5000 and then the bot restarts, the next sweep only fetches items 5001+ — typically just a few pages instead of hundreds.

### Where the watermark lives

- **Locally**: in `state.json` (next to `last_offset_id` field)
- **On Render free tier**: `state.json` is wiped on redeploy, so the watermark resets too — UNLESS you set `USE_TELEGRAM_STATE_SYNC=1` (mirrors to Saved Messages) OR upgrade to a paid Disk.

### Resetting the watermark

If you want the bot to re-scan from the beginning (e.g., you deleted items from the destination channel and want to re-send them):

**Via web UI:** click the blue **"Reset watermark"** button at `/`

**Via HTTP:**
```bash
curl -X POST https://your-service.onrender.com/reset
```

**Via direct state.json edit:** set `last_offset_id` to `0` in `state.json` and restart.

> Note: resetting the watermark only re-scans from id=1; items already marked as `sent_ids` will still be skipped (idempotency). If you want to FORCE re-sending of everything, delete `state.json` entirely.

### Viewing the current watermark

- **Web UI**: shown at `/` in the Controls card: `last_offset_id=N`
- **JSON API**: `curl https://your-service.onrender.com/status | jq .last_offset_id`
- **Logs**: `[sweep] auto-resuming from id > N (last processed in prior sweep)` on each sweep

---

## Burst pacing model (~50 items/minute)

The bot sends media in **bursts** rather than trickling one item at a time:

```
Time →    0s         25s        35s         60s        85s       95s       120s
         ├───────────┼───────────┼───────────┼──────────┼──────────┼─────────┤
Burst 1: ████████████████████                          
         50 items (0.5s each)                          
                          wait 35s (until 60s elapsed since burst 1 start)
                                              Burst 2: ████████████████████
                                                       50 more items
                                                                wait 35s
                                                                            ...
```

### Why burst instead of steady trickle?

- Telegram's rate limit per chat is roughly **30 messages per second burst, 20 msg/min sustained**.
- Trickle (1 every 1.2s = 50/min) hits the sustained limit AND feels slow.
- Burst (50 in 25s) is well under the burst limit, then idle for 35s = averages 50/min safely.
- You get visible progress quickly (50 items land within 25s), then a brief pause, then another burst.

### Tuning the pacing

| Goal | Settings |
|---|---|
| Default: ~50 items/min | `BATCH_SIZE=50` `PER_MESSAGE_DELAY=0.5` `BATCH_INTERVAL_SEC=60` |
| Faster (~100 items/min) | `BATCH_INTERVAL_SEC=30` (keep others same) |
| Even faster (~150 items/min) | `BATCH_INTERVAL_SEC=20` `PER_MESSAGE_DELAY=0.3` (risky — watch for FloodWait) |
| Slower & safer (~25 items/min) | `BATCH_INTERVAL_SEC=120` |
| Maximum throughput per burst | `PER_MESSAGE_DELAY=0.1` (10 msg/sec — at Telegram's per-chat limit) |

> ⚠️ **Don't set `PER_MESSAGE_DELAY` below 0.1** — you'll trigger FloodWait and the bot will be forced to wait 20+ seconds between bursts anyway.

### What happens during a burst pause?

During the ~35s idle between bursts, the bot:
1. Saves `state.json` (so progress isn't lost on crash)
2. Updates the web UI's progress display
3. Checks for stop signals (POST /stop, Ctrl+C, SIGTERM)
4. Sleeps in 1-second increments so it can react to a stop signal within 1s

You can watch the countdown live at the web UI: `⏸ Batch #1 pause [██████░░░░] 23s remaining`.

### Picking up new items

The bot re-scans Saved Messages every 60s after a sweep completes. With auto-resume, the re-scan only fetches items with `id > last_offset_id` — so if you saved 5 new photos since the last sweep, the next sweep grabs just those 5 (in ~1 second) and forwards them in the next burst cycle.

If you're catching up on a large backlog (e.g. 10,000 saved items), the bot will be busy for ~200 minutes processing the backlog. New items you save during this time will be picked up after the backlog is processed, on the next re-scan.

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

### `Waiting for 24 seconds before continuing (required by "messages.GetHistory")`

This is Telegram imposing a `FloodWait` penalty because the bot is calling `get_chat_history` too fast during the initial scan (when `ORDER=old`). Telegram limits `GetHistory` to ~30 calls per 30 seconds per account.

The bot now has a `PAGE_DELAY` env var (default `0.4` seconds) that throttles pagination calls to stay under the limit. If you still see this message:

| Cause | Fix |
|---|---|
| `PAGE_DELAY` is `0` or too low | Set `PAGE_DELAY=0.5` or higher in Render env vars. |
| You have a huge Saved Messages and `MAX_SCAN` is `0` (unlimited) | Set `MAX_SCAN=5000` (or lower) to cap the scan. |
| Your account was already rate-limited from another client | Wait 5–10 minutes for the limit to clear, then redeploy. |

The `Waiting for 24 seconds…` message itself is **not fatal** — Pyrogram waits the requested time and then retries automatically. But it makes the scan slow. Set `PAGE_DELAY=0.5` to prevent it.

### `PeerIdInvalid: [400 PEER_ID_INVALID] - The peer id being used is invalid or not known yet`

This error means Pyrogram's session doesn't have an access hash cached for your target channel/group. The bot now calls `client.get_chat(target)` at startup to resolve the peer and cache the access hash automatically — so this error should NOT happen anymore on fresh deploys.

If you still see it, the cause is one of:

| Cause | Fix |
|---|---|
| **Target is a private channel/group you're not a member of** | Open the channel in your Telegram app and join it (or ask an admin to add you), then redeploy. |
| **Target is a `@username` that doesn't exist** | Check the spelling. Telegram usernames are case-insensitive but must match otherwise. |
| **Target is a numeric id like `-1001234567890` but you've never opened the chat from this account** | Open the chat once from your Telegram app (so your account has "met" the peer), then redeploy. |
| **Numeric id missing the `-100` prefix** | For supergroups/channels, the id MUST be in the form `-100<id>` (e.g., `-1001234567890`). Without `-100`, Telegram treats it as a user id. |

The startup logs will show `✓ target resolved: 'channel name' (id=…)` when the peer is successfully resolved, or a friendly error explaining what's wrong.

### `BATCH_PAUSE_MIN` / `BATCH_PAUSE_MAX` env vars on Render

These are **legacy** env vars from an older version of the bot. They are no longer used. The new pacing model uses a single env var: `BATCH_INTERVAL_SEC` (default 60s).

If you have `BATCH_PAUSE_MIN` or `BATCH_PAUSE_MAX` set on Render, **delete them** to avoid confusion. Their values are silently ignored.

### Other common issues

| Symptom | Likely cause / fix |
|---|---|
| `SESSION_STRING` rejected on Render | Run `session_setup.py` again locally — sessions can expire. |
| `FloodWait` constantly during sending | Lower `BATCH_SIZE` or raise `PER_MESSAGE_DELAY`. Telegram may also be throttling your account globally. |
| `FloodWait` during scan (the `Waiting for N seconds before continuing (required by "messages.GetHistory")` message) | Raise `PAGE_DELAY` to `0.5` or higher, and/or lower `MAX_SCAN`. |
| Bot collects messages forever without sending | Likely `MAX_SCAN=0` (unlimited) on a huge Saved Messages. Set `MAX_SCAN=5000` to cap. |
| `PeerIdInvalid` for target | Make sure you've opened the target channel once from your account, or use the `-100…` numeric id. |
| Bot is running but nothing forwards | Check the `FILTER` — items in Saved Messages that are stickers, voice notes, or documents don't match any filter. |
| State resets on Render redeploy | Expected behavior by default. Set `USE_TELEGRAM_STATE_SYNC=1` to mirror state to Saved Messages, OR upgrade to a paid Render Disk. |
| Album comes through split into single photos | Telegram's album grouping can be lost if any item in the album was forwarded/deleted before fetch. The bot will retry the next sweep. |
| Service keeps sleeping on free tier | Render free web services sleep after 15 min of no inbound HTTP. Set up UptimeRobot to ping `https://your-service.onrender.com/health` every 10 min. |

---

## License

MIT — do whatever you want. No warranty. Be a good Telegram citizen and don't abuse rate limits.
