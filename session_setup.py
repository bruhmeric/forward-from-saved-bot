"""
One-time local script to generate a Pyrogram StringSession.

Usage (local, NOT on Render):
    python session_setup.py

You'll be prompted for:
    - API_ID  (from https://my.telegram.org → API Development Tools)
    - API_HASH (same place)
    - phone number (with country code, e.g. +44…)
    - Telegram login code (sent to your Telegram app)
    - 2FA password if you have one

The script prints a SESSION_STRING line — paste that into Render's
Environment tab (or your local .env) as SESSION_STRING=… and you're done.
"""
from __future__ import annotations

import asyncio
import sys


async def main() -> None:
    try:
        from pyrogram import Client
        from pyrogram.errors import SessionRevoked, AuthKeyUnregistered
    except ImportError:
        print("Missing pyrogram. Run: pip install -r requirements.txt")
        sys.exit(1)

    print("=" * 70)
    print("Telegram Saved-Messages bulk forwarder — session setup")
    print("=" * 70)
    print("Get API_ID and API_HASH from: https://my.telegram.org/apps\n")

    api_id_str = input("API_ID: ").strip()
    api_hash   = input("API_HASH: ").strip()
    phone      = input("Phone (with country code, e.g. +447700900000): ").strip()

    if not (api_id_str and api_hash and phone):
        print("All three fields are required.")
        sys.exit(1)

    try:
        api_id = int(api_id_str)
    except ValueError:
        print(f"API_ID must be numeric, got {api_id_str!r}")
        sys.exit(1)

    # Use a fresh in-memory session for export.
    from pyrogram.session.auth import Auth  # noqa: F401  (sanity check import works)

    app = Client(
        name="session_setup",            # ignored — we use StringSession below
        api_id=api_id,
        api_hash=api_hash,
        phone_number=phone,
        in_memory=True,                  # don't write a .session file
    )

    print("\nConnecting — Telegram may send you a login code. Reply with it when prompted.")
    try:
        await app.start()
    except (SessionRevoked, AuthKeyUnregistered) as e:
        print(f"\nAuth failed: {e!r}. Generate a fresh API session.")
        sys.exit(1)
    except Exception as e:
        print(f"\nLogin failed: {e!r}")
        sys.exit(1)

    try:
        me = await app.get_me()
        print(f"\nLogged in as: {me.first_name} (@{me.username or '—'}) id={me.id}")

        # Export the string session.
        from pyrogram import types  # noqa: F401
        # Pyrogram 2.x: use await app.export_session_string()
        try:
            session_str = await app.export_session_string()
        except AttributeError:
            # Older API: session.save() returns the string synchronously.
            session_str = app.export_session_string()

        print("\n" + "=" * 70)
        print("✅ SESSION_STRING (copy this entire line, including the =):")
        print("=" * 70)
        print(f"SESSION_STRING={session_str}")
        print("=" * 70)
        print("\nPaste that into your Render env vars (or local .env).")
        print("Then deploy the worker — no further login needed.")
    finally:
        await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
