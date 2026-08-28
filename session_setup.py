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

The script prints a SESSION_STRING value on its own line. Copy ONLY the
string (NOT the "SESSION_STRING=" prefix) into Render's Environment tab.
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

    app = Client(
        name="session_setup",
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

        # Pyrogram 2.x: export_session_string() is SYNC, returns str directly.
        # Do NOT await it — awaiting a str raises TypeError.
        session_str = app.export_session_string()

        # Verify the session string is well-formed (Pyrogram v2 expects ~370 chars).
        if not session_str or len(session_str) < 200:
            print(f"\n⚠️  WARNING: session string looks too short ({len(session_str)} chars). "
                  "Login may not have completed properly.")
            sys.exit(1)

        print()
        print("=" * 70)
        print("✅ SESSION_STRING generated successfully!")
        print("=" * 70)
        print()
        print("COPY ONLY THE LINE BELOW (no quotes, no 'SESSION_STRING=' prefix):")
        print()
        print(session_str)
        print()
        print("=" * 70)
        print()
        print("HOW TO USE IT:")
        print()
        print("  • In Render → Environment → add a new var:")
        print("      Key:   SESSION_STRING")
        print("      Value: <paste the line above, exactly as-is>")
        print()
        print("  • In local .env:")
        print("      SESSION_STRING=<paste the line above>")
        print()
        print("DO NOT:")
        print("  ✗ include 'SESSION_STRING=' in the Value field (Render adds that)")
        print("  ✗ wrap it in quotes")
        print("  ✗ add spaces at the start/end")
        print()
        print(f"Length: {len(session_str)} chars · Format: Pyrogram v2 StringSession")
    finally:
        await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
