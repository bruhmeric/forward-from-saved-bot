#!/usr/bin/env bash
# setup.sh — one-time setup for the dual-bot VPS deployment
#
# This script:
#   1. Clones the forward-from-saved-bot from GitHub
#   2. Creates .env files for both bots (from .env.example templates)
#   3. Reminds you to fill in API_ID, API_HASH, SESSION_STRING, etc.
#   4. Reminds you to generate SESSION_STRING locally
#
# After running this script:
#   1. Edit telegram-forwarder-bot/.env and fill in your credentials
#   2. Edit forward-from-saved-bot/.env and fill in your credentials
#   3. Run: docker-compose up -d

set -e

echo "============================================"
echo "  Dual Telegram Bot VPS Setup"
echo "============================================"
echo ""

# --- Step 1: Clone the second bot ---
echo "[1/4] Cloning forward-from-saved-bot from GitHub..."

if [ -d "forward-from-saved-bot" ]; then
    echo "  ✓ forward-from-saved-bot/ already exists — skipping clone"
else
    git clone https://github.com/bruhmeric/forward-from-saved-bot.git forward-from-saved-bot
    echo "  ✓ Cloned successfully"
fi

# --- Step 2: Create .env files ---
echo ""
echo "[2/4] Creating .env files..."

# Forwarder bot .env
if [ -f "telegram-forwarder-bot/.env" ]; then
    echo "  ✓ telegram-forwarder-bot/.env already exists — skipping"
else
    cp telegram-forwarder-bot/.env.example telegram-forwarder-bot/.env
    echo "  ✓ Created telegram-forwarder-bot/.env"
fi

# Saved forwarder .env
if [ -f "forward-from-saved-bot/.env" ]; then
    echo "  ✓ forward-from-saved-bot/.env already exists — skipping"
else
    # The second bot may not have a .env.example — create one from README info
    cat > forward-from-saved-bot/.env << 'ENVEOF'
# === Telegram API credentials ===
# Get these from https://my.telegram.org/apps
API_ID=
API_HASH=

# === Session string ===
# Generate locally with: python session_setup.py
# (run this INSIDE the forward-from-saved-bot directory)
SESSION_STRING=

# === Destination chat ===
# The chat/group/channel where media from Saved Messages will be forwarded
# Can be: @username, -1001234567890 (channel ID), or "me" (Saved Messages)
TARGET=-1001234567890

# === Optional settings (defaults shown) ===
# Filter: photo, video, animation, or all (default: all)
# FILTER=all

# Order: old (oldest first) or new (newest first, default: old)
# ORDER=old

# Burst pacing (see README for tuning)
# BATCH_SIZE=50
# PER_MESSAGE_DELAY=0.5
# BATCH_INTERVAL_SEC=60

# Web server port (Render sets PORT automatically; on VPS, set it)
PORT=10000

# State file location (persisted in Docker volume)
STATE_FILE=/app/data/state.json
PROGRESS_MESSAGE_ID_FILE=/app/data/progress_msg_id.txt

# Max messages to scan per sweep (0 = unlimited)
# Default: 11000 (covers most Saved Messages in one sweep)
# DO NOT set this too low (e.g. 10) — the bot will only process 10 items
# per sweep, then wait 60s before the next sweep.
# MAX_SCAN=0

# Optional: mirror progress to Saved Messages
# TELEGRAM_PROGRESS=0

# Optional: mirror state.json to Saved Messages (free-tier persistence)
# USE_TELEGRAM_STATE_SYNC=0
ENVEOF
    echo "  ✓ Created forward-from-saved-bot/.env"
fi

# --- Step 3: Remind user to fill in credentials ---
echo ""
echo "[3/4] ACTION REQUIRED: Fill in your credentials"
echo ""
echo "  You need to edit TWO .env files:"
echo ""
echo "  1. telegram-forwarder-bot/.env"
echo "     Required: BOT_TOKEN, API_ID, API_HASH, SESSION_STRING"
echo "     Optional: DESTINATION_GROUP_ID, ADMIN_IDS"
echo ""
echo "  2. forward-from-saved-bot/.env"
echo "     Required: API_ID, API_HASH, SESSION_STRING, TARGET"
echo "     (TARGET = @username or -1001234567890 — NOT DEST_CHAT_ID)"
echo ""
echo "  Both bots can use the SAME SESSION_STRING (same Telegram account)."
echo ""

# --- Step 4: Remind about SESSION_STRING generation ---
echo "[4/4] Generating SESSION_STRING"
echo ""
echo "  If you already have a SESSION_STRING (from Render), reuse it."
echo "  Otherwise, generate one locally:"
echo ""
echo "  For the forwarder bot:"
echo "    cd telegram-forwarder-bot"
echo "    pip install -r requirements.txt"
echo "    python login.py --string"
echo "    (copy the printed string)"
echo ""
echo "  For the saved-forwarder bot:"
echo "    cd forward-from-saved-bot"
echo "    pip install -r requirements.txt"
echo "    python session_setup.py"
echo "    (copy the printed string)"
echo ""
echo "  Both produce Telethon StringSessions — same format, interchangeable."
echo "  You can use ONE string for both bots."
echo ""

echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "  Next steps:"
echo "    1. Edit both .env files with your credentials"
echo "    2. Start both bots:"
echo "       docker-compose up -d"
echo "    3. Watch logs:"
echo "       docker-compose logs -f"
echo "    4. Check the saved-forwarder web dashboard:"
echo "       http://localhost:10000"
echo ""
echo "  Useful commands:"
echo "    docker-compose stop        — stop both bots"
echo "    docker-compose restart     — restart both bots"
echo "    docker-compose logs forwarder-bot    — only forwarder bot logs"
echo "    docker-compose logs saved-forwarder  — only saved forwarder logs"
echo ""
