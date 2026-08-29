# Dual Telegram Bot VPS Deployment

Run **two Telegram bots** + a **unified dashboard** on a single VPS using Docker:

1. **Telegram Forwarder Bot** — forwards media to topics/groups, scrapes channels, pulls from locked private channels
2. **Forward-from-Saved-Bot** — pulls media from your Saved Messages and forwards it to a destination chat in batches
3. **Dashboard** — web UI showing both bots' live stats, with controls to stop scrapes, clear captions, stop/reset the saved-forwarder

All three run in separate Docker containers, sharing the same VPS.

---

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │              Your VPS                     │
                    │                                          │
   Port 8080  ───►  │  ┌──────────────┐                        │
   (Dashboard)      │  │  Dashboard   │──┐                     │
                    │  └──────────────┘  │                     │
                    │                     │  Docker network     │
                    │  ┌──────────────┐  │  (internal)         │
                    │  │ Forwarder    │──┘                      │
                    │  │ Bot (:8081)  │◄─── stats               │
                    │  └──────────────┘                         │
                    │                     │                     │
                    │  ┌──────────────┐  │                     │
 Port 10000  ───►  │  │ Saved        │──┘                     │
 (Saved UI)        │  │ Forwarder    │                        │
                    │  │ (:10000)     │                        │
                    │  └──────────────┘                        │
                    └─────────────────────────────────────────┘
```

- **Port 8080** — Unified Dashboard (shows both bots)
- **Port 10000** — Saved-forwarder's built-in web UI (progress/batch view)
- Forwarder bot's stats server (port 8081) is internal-only (Docker network)

---

## Prerequisites

- A VPS with **Docker** and **Docker Compose** installed
- A **Telegram bot token** (from @BotFather) — only needed for the Forwarder Bot
- **Telegram API credentials** (API_ID + API_HASH from https://my.telegram.org/apps)
- A **SESSION_STRING** (Telethon user session — both bots can share the same one)

### Installing Docker on your VPS (if not already installed)

```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in for the docker group to take effect

# Verify
docker --version
docker-compose --version
```

---

## Quick Start (5 steps)

### Step 1. Clone this repo to your VPS

```bash
git clone https://github.com/YOUR_USERNAME/telegram-bots-vps.git
cd telegram-bots-vps
```

### Step 2. Run the setup script

```bash
chmod +x setup.sh
./setup.sh
```

This will:
- Clone the `forward-from-saved-bot` from GitHub into `./forward-from-saved-bot/`
- Create `.env` files for both bots (from templates)

### Step 3. Generate SESSION_STRING (if you don't have one from Render)

If you already have a `SESSION_STRING` from your Render deployment, reuse it.

Otherwise, generate one locally (on your laptop, NOT on the VPS — you need to receive the Telegram login code):

```bash
# Option A: Use the forwarder bot's login script
cd telegram-forwarder-bot
pip install -r requirements.txt
python login.py --string
# Copy the printed string (starts with "1")

# Option B: Use the saved-forwarder's session setup
cd ../forward-from-saved-bot
pip install -r requirements.txt
python session_setup.py
# Copy the printed string
```

Both produce the same Telethon StringSession format — you can use ONE string for both bots.

### Step 4. Edit the .env files

Edit **both** `.env` files with your credentials:

```bash
nano telegram-forwarder-bot/.env    # Forwarder Bot config
nano forward-from-saved-bot/.env    # Saved Messages Forwarder config
```

#### telegram-forwarder-bot/.env (required fields):

```env
# From @BotFather
BOT_TOKEN=123456789:ABC-DEF...

# From my.telegram.org
API_ID=1234567
API_HASH=abcdef1234567890abcdef1234567890

# From login.py --string (or session_setup.py)
SESSION_STRING=1BVtsOH8Bu...

# Destination group ID (optional — use /saved or /scrape saved instead)
# DESTINATION_GROUP_ID=-1001234567890

# Your Telegram user ID (optional — for admin whitelist)
# ADMIN_IDS=123456789

# Run mode — MUST be polling on VPS (no webhook)
MODE=polling

# Database path (inside the Docker container)
DB_PATH=/app/data/forwarder.db
```

#### forward-from-saved-bot/.env (required fields):

```env
# From my.telegram.org (same as above)
API_ID=1234567
API_HASH=abcdef1234567890abcdef1234567890

# Same session string as above (same Telegram account)
SESSION_STRING=1BVtsOH8Bu...

# Destination chat — use TARGET (NOT DEST_CHAT_ID!)
# Can be: @username, -1001234567890, or "me"
TARGET=-1001234567890

# Web dashboard port
PORT=10000

# State file (persisted in Docker volume)
STATE_FILE=/app/data/state.json
PROGRESS_MESSAGE_ID_FILE=/app/data/progress_msg_id.txt
```

> ⚠️ **Note**: The second bot uses `TARGET` (not `DEST_CHAT_ID`). The setup.sh script creates the .env with the correct variable name.

### Step 5. Start all services

```bash
docker-compose up -d
```

That's it! All three services are now running.

---

## Accessing the Dashboard

### Unified Dashboard (recommended)

```
http://YOUR_VPS_IP:8080
```

Shows:
- **Forwarder Bot** status (online/offline, Telethon connection, destination, custom caption)
- **Forwarder Bot** scrape progress (if active: sent/failed/skipped, source, elapsed time)
- **Saved Forwarder** status (running/idle, sweep count, total sent, last message ID)
- Control buttons: Stop Scrape, Clear Caption, Stop Saved Forwarder, Reset Watermark

Auto-refreshes every 5 seconds.

### Saved Forwarder's built-in UI (optional)

```
http://YOUR_VPS_IP:10000
```

The second bot's own web UI with detailed batch pacing progress, sweep info, and reset button.

---

## Monitoring & Management

### View logs (all services)

```bash
docker-compose logs -f
```

### View logs (one service only)

```bash
docker-compose logs -f forwarder-bot      # Forwarder Bot
docker-compose logs -f saved-forwarder    # Saved Messages Forwarder
docker-compose logs -f dashboard          # Dashboard
```

### Check running containers

```bash
docker-compose ps
```

### Stop all services

```bash
docker-compose stop
```

### Restart all services

```bash
docker-compose restart
```

### Rebuild after code changes

```bash
docker-compose up -d --build
```

---

## Bot 1: Telegram Forwarder Bot

**What it does:**
- Forward any media you send to a destination group/topic
- Pull content from locked private channels via t.me links
- Auto-scrape entire channels (`/scrape`)
- Send directly to Saved Messages (`/saved`)
- Custom captions (`/caption`)
- Media type filters (`/scrape <url> photo video`)

**Commands:** Send `/help` to the bot on Telegram for the full list.

**Key commands:**
| Command | Action |
|---|---|
| `/start` | Show intro |
| `/help` | Show all commands |
| `/setgroup <id>` | Set destination group/channel |
| `/saved <url>` | Send t.me link content to Saved Messages |
| `/scrape <url> [flags]` | Scrape ALL media from a channel |
| `/caption <text>` | Set custom caption for all forwards |
| `/status` | Show bot status |

**Run mode:** Polling (no webhook needed on VPS — the bot connects to Telegram directly)

**Stats endpoint:** `http://forwarder-bot:8081/stats` (internal Docker network only)

---

## Bot 2: Forward-from-Saved-Bot

**What it does:**
- Iterates your Saved Messages (oldest-first by default)
- Forwards all photos/videos/animations to a destination chat
- Strips captions and "forwarded from" headers
- Sends in 50-item bursts (~50 items/minute)
- Auto-resumes from where it left off (state.json)
- Web dashboard at port 10000 for live progress
- `POST /stop` endpoint to halt remotely

**GitHub:** https://github.com/bruhmeric/forward-from-saved-bot

**Key env vars:**
| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API hash |
| `SESSION_STRING` | Telethon session (same as Bot 1) |
| `TARGET` | Destination chat (`@username`, `-1001234567890`, or `me`) |
| `FILTER` | `photo`, `video`, `animation`, or `all` (default: all) |
| `ORDER` | `old` (oldest first) or `new` (newest first, default: old) |
| `BATCH_SIZE` | Items per burst (default: 50) |
| `PER_MESSAGE_DELAY` | Delay between items in a burst (default: 0.5s) |
| `BATCH_INTERVAL_SEC` | Seconds between bursts (default: 60) |
| `MAX_SCAN` | Max messages to fetch per sweep. 0 = unlimited (default: 11000). **Don't set this too low** — see below. |

> ⚠️ **MAX_SCAN too low**: If you set `MAX_SCAN=10` and the bot stopped forwarding after 10 items, that's because it only fetches 10 messages per sweep. Fix:
> 1. Edit `forward-from-saved-bot/.env` → set `MAX_SCAN=0` (unlimited) or `MAX_SCAN=5000`
> 2. Restart: `docker-compose restart saved-forwarder`
> 3. Reset the watermark so it re-scans from the beginning: click "Reset Watermark" in the dashboard, or `curl -X POST http://localhost:10000/reset`
>
> ⚠️ **Render compatibility**: This bot was originally designed for Render. On VPS, it works the same way — it reads `PORT` from env (defaults to 10000) and serves its web UI + health check on that port. No changes needed to the bot itself. The `.env` file created by `setup.sh` includes the correct `STATE_FILE` and `PROGRESS_MESSAGE_ID_FILE` paths for Docker volumes.

---

## Dashboard

**What it does:**
- Fetches stats from both bots every 5 seconds
- Displays them in a clean dark-themed web UI
- Provides control buttons for common actions

**Endpoints:**
| Endpoint | Method | Action |
|---|---|---|
| `/` | GET | HTML dashboard |
| `/api/stats` | GET | JSON stats from both bots |
| `/api/health` | GET | Dashboard health check |
| `/api/stop_scrape` | POST | Stop active scrape (Forwarder Bot) |
| `/api/cancel_caption` | POST | Clear custom caption (Forwarder Bot) |
| `/api/stop_saved` | POST | Stop Saved Forwarder |
| `/api/reset_saved` | POST | Reset Saved Forwarder watermark |

**Controls available from the dashboard:**
- Stop active scrape
- Clear custom caption
- Stop the Saved Messages Forwarder
- Reset the Saved Messages Forwarder watermark (re-scan from beginning)

---

## Using the Same Telegram Account for Both Bots

Both bots use a Telethon **StringSession** tied to your personal Telegram account. Using the same `SESSION_STRING` for both is safe because:

- StringSession contains only the auth key (not entity cache state)
- Telegram allows multiple concurrent connections from the same account
- Each bot runs in its own Docker container with its own Telethon client instance

If you experience any issues (rare), generate a second `SESSION_STRING` for the second bot using a secondary Telegram account.

---

## Updating

### Update the Forwarder Bot (Bot 1)

If you modify the forwarder bot's code:

```bash
docker-compose up -d --build forwarder-bot
```

### Update the Saved Forwarder (Bot 2)

If you want to pull the latest version from GitHub:

```bash
cd forward-from-saved-bot
git pull
cd ..
docker-compose up -d --build saved-forwarder
```

### Update the Dashboard

```bash
docker-compose up -d --build dashboard
```

### Update Everything

```bash
cd forward-from-saved-bot && git pull && cd ..
docker-compose up -d --build
```

---

## Troubleshooting

### "Cannot connect to Telegram" / entity resolution errors

The forwarder bot automatically refreshes its dialog cache when it can't find an entity. The first request after a restart takes ~2 seconds longer. This is normal.

### Dashboard shows bots as offline

- Check if containers are running: `docker-compose ps`
- Check logs: `docker-compose logs forwarder-bot`
- The dashboard queries internal Docker DNS (`forwarder-bot`, `saved-forwarder`) — this only works when all containers are on the same `bots-network`

### Bot 2 (saved-forwarder) won't start

Check the env vars:
```bash
docker-compose logs saved-forwarder | head -20
```

Common issues:
- Missing `TARGET` (should be `@username`, `-100...`, or `me`)
- Missing `API_ID` / `API_HASH` / `SESSION_STRING`
- `ORDER` must be `old` or `new` (not `oldest` / `newest`)

### Firewall

Open the dashboard port:
```bash
sudo ufw allow 8080/tcp    # Dashboard
sudo ufw allow 10000/tcp   # Saved Forwarder UI (optional)
```

### Docker permission denied

```bash
sudo usermod -aG docker $USER
# Log out and back in
```

---

## File Structure

```
telegram-bots-vps/
├── telegram-forwarder-bot/       # Bot 1: Forwarder Bot
│   ├── bot.py                    # Includes /stats endpoint for dashboard
│   ├── config.py
│   ├── db.py
│   ├── user_session.py
│   ├── topics.py
│   ├── login.py
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── admin.py
│   │   ├── direct.py
│   │   └── link.py
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── .env.example
│   └── .env                       # ← you fill this in
│
├── forward-from-saved-bot/        # Bot 2: Saved Messages Forwarder
│   ├── main.py                    # (cloned by setup.sh from GitHub)
│   ├── forwarder.py
│   ├── config.py
│   ├── ...                        # (all files from GitHub repo)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env                       # ← you fill this in
│
├── dashboard/                     # Unified Dashboard
│   ├── dashboard.py               # aiohttp web server (port 8080)
│   ├── requirements.txt
│   └── Dockerfile
│
├── docker-compose.yml             # Runs all 3 services
├── setup.sh                       # One-time setup script
├── .gitignore
└── README.md                      # This file
```

---

## Cost

- **VPS**: Any cheap VPS ($3-5/month) works — both bots + dashboard use minimal CPU/RAM
- **Telegram**: Free (both use your personal account, not paid bot API)
- **Docker**: Free

Recommended VPS specs:
- 1 vCPU
- 1 GB RAM
- 10 GB disk
- Ubuntu 22.04 or 24.04
