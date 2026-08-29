# Custom Dockerfile for forward-from-saved-bot that doesn't hardcode STATE_FILE.
#
# The upstream Dockerfile hardcodes `STATE_FILE=/tmp/state.json` which
# overrides the .env file's STATE_FILE=/app/data/state.json. On Docker,
# /tmp is ephemeral — state is lost on every restart, causing the watermark
# to reset and re-sending all items.
#
# This Dockerfile is identical to the upstream one but:
# 1. Removes the hardcoded STATE_FILE and PROGRESS_MESSAGE_ID_FILE env vars
#    so the .env file's values (pointing to /app/data/) take effect
# 2. Creates /app/data/ directory for the Docker volume mount
# 3. Uses Python 3.12 instead of 3.11 (matches the forwarder bot)
#
# The upstream Dockerfile (from https://github.com/bruhmeric/forward-from-saved-bot)
# is designed for Render, where /tmp is the only writable path on the free tier.
# On a VPS with Docker volumes, we want state in the persistent volume at /app/data/.

FROM python:3.12-slim

# System deps for cryptg (compiled C extension).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source.
COPY . .

# Create data directory for persistent state (Docker volume mounts here)
RUN mkdir -p /app/data

# Do NOT hardcode STATE_FILE or PROGRESS_MESSAGE_ID_FILE here —
# let the .env file set them (STATE_FILE=/app/data/state.json).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 10000

# Start the bot
CMD ["python", "main.py"]
