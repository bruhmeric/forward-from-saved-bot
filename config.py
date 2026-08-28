"""
Config loading: env vars (Render-friendly) with CLI flag overrides.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Set

from dotenv import load_dotenv

# Only load .env if present (local dev). On Render, env vars are injected.
load_dotenv()


@dataclass
class Config:
    # Telegram credentials
    api_id: int
    api_hash: str
    session_string: str

    # Destination
    target: str

    # Filtering
    filter_types: Set[str]            # subset of {"photo","video","animation"}

    # Behaviour
    order: str                        # "new" or "old"
    batch_size: int
    per_message_delay: float          # seconds between individual messages
    batch_pause_min: int              # seconds pause after each batch of N
    batch_pause_max: int
    stop_poll_interval: int           # seconds between Saved Messages polls

    # State
    state_file: str

    # Telegram-side live progress reporter
    telegram_progress: bool = True        # post live progress to a chat
    progress_chat: str = "me"             # 'me' = Saved Messages, or @username / -100…
    progress_update_interval: float = 5.0 # throttle (seconds between edits)
    progress_message_id_file: str = ""    # where to persist the live message_id

    def __post_init__(self) -> None:
        if self.order not in ("new", "old"):
            raise ValueError(f"ORDER must be 'new' or 'old', got {self.order!r}")
        valid = {"photo", "video", "animation"}
        bad = self.filter_types - valid
        if bad:
            raise ValueError(f"Unknown filter types {bad}. Valid: {valid}")
        if not self.filter_types:
            raise ValueError("FILTER is empty — choose at least one of photo,video,animation")
        if self.batch_size < 1 or self.batch_size > 50:
            raise ValueError("BATCH_SIZE must be between 1 and 50")
        if self.batch_pause_min > self.batch_pause_max:
            raise ValueError("BATCH_PAUSE_MIN must be <= BATCH_PAUSE_MAX")
        if self.progress_update_interval < 2.0:
            # Telegram rate-limits message edits; keep >=2s to avoid FloodWait.
            raise ValueError("PROGRESS_UPDATE_INTERVAL must be >= 2.0 seconds")
        if not self.progress_chat:
            raise ValueError("PROGRESS_CHAT must be set (use 'me' for Saved Messages)")


def _parse_filter(raw: str) -> Set[str]:
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return set(parts) if parts else {"photo", "video", "animation"}


def from_env(cli_overrides: dict | None = None) -> Config:
    """Build Config from env vars, with optional CLI flag overrides."""
    cli = cli_overrides or {}

    api_id_raw = cli.get("api_id") or os.environ.get("API_ID")
    api_hash   = cli.get("api_hash") or os.environ.get("API_HASH") or ""
    sess       = cli.get("session_string") or os.environ.get("SESSION_STRING") or ""
    target     = cli.get("target") or os.environ.get("TARGET") or ""
    filter_raw = cli.get("filter") or os.environ.get("FILTER") or "photo,video,animation"
    order      = (cli.get("order") or os.environ.get("ORDER") or "new").lower()
    batch_size = int(cli.get("batch_size") or os.environ.get("BATCH_SIZE") or "50")
    per_msg    = float(cli.get("per_message_delay") or os.environ.get("PER_MESSAGE_DELAY") or "2.5")
    bp_min     = int(cli.get("batch_pause_min") or os.environ.get("BATCH_PAUSE_MIN") or "120")
    bp_max     = int(cli.get("batch_pause_max") or os.environ.get("BATCH_PAUSE_MAX") or "180")
    stop_int   = int(cli.get("stop_poll_interval") or os.environ.get("STOP_POLL_INTERVAL") or "5")
    state_file = cli.get("state_file") or os.environ.get("STATE_FILE") or "./state.json"

    # Telegram-side live progress (defaults ON, posts to Saved Messages)
    tp_raw     = (cli.get("telegram_progress") or os.environ.get("TELEGRAM_PROGRESS") or "1").strip().lower()
    telegram_progress = tp_raw in ("1", "true", "yes", "on")
    progress_chat     = cli.get("progress_chat") or os.environ.get("PROGRESS_CHAT") or "me"
    progress_interval = float(cli.get("progress_update_interval") or os.environ.get("PROGRESS_UPDATE_INTERVAL") or "5.0")
    progress_msg_file = cli.get("progress_message_id_file") or os.environ.get("PROGRESS_MESSAGE_ID_FILE") or ""
    if not progress_msg_file:
        # Put it next to state.json so a Render Disk mount at /data picks it up too.
        base = os.path.dirname(os.path.abspath(state_file)) or "."
        progress_msg_file = os.path.join(base, "progress_msg_id.txt")

    if not api_id_raw:
        raise SystemExit("Missing API_ID. Set it in env or pass --api-id.")
    if not api_hash:
        raise SystemExit("Missing API_HASH. Set it in env or pass --api-hash.")
    if not sess:
        raise SystemExit(
            "Missing SESSION_STRING. Run `python session_setup.py` locally first, "
            "then paste the printed string into your env vars."
        )
    if not target:
        raise SystemExit("Missing TARGET. Pass --target=@channel or set TARGET env var.")

    return Config(
        api_id=int(api_id_raw),
        api_hash=api_hash,
        session_string=sess,
        target=target,
        filter_types=_parse_filter(filter_raw),
        order=order,
        batch_size=batch_size,
        per_message_delay=per_msg,
        batch_pause_min=bp_min,
        batch_pause_max=bp_max,
        stop_poll_interval=stop_int,
        state_file=state_file,
        telegram_progress=telegram_progress,
        progress_chat=progress_chat,
        progress_update_interval=progress_interval,
        progress_message_id_file=progress_msg_file,
    )
