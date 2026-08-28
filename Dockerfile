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

# Render auto-sets PORT; we expose 10000 as documentation (Render may assign another).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_FILE=/tmp/state.json \
    PROGRESS_MESSAGE_ID_FILE=/tmp/progress_msg_id.txt
EXPOSE 10000

# Render WEB SERVICE: binds to $PORT and serves /health for the platform check.
CMD ["python", "main.py"]
