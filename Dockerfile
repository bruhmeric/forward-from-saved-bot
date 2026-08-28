# Lightweight Python image — Render uses this if you specify Docker.
FROM python:3.11-slim

# System deps for tgcrypto (compiled C extension).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source.
COPY . .

# Stateless-friendly defaults: /tmp for transient files, /data for state.json (mounted disk).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_FILE=/data/state.json

# Render Background Worker: just run the main script.
CMD ["python", "main.py"]
