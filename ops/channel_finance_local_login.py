"""One-time LOCAL QR session provisioning; never run this inside Telegram bots.

The operator executes this on a trusted workstation, scans the displayed QR
using their own Telegram app, and then transfers the file through their own
secret-management channel. ViMi Pay never asks for login codes in bot chats.
The resulting session DOES have full Telegram account privileges. Handle it
as a highly privileged secret, regardless of the read-only revenue agent.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path


async def _create_session(
    *, api_id: int, api_hash: str, expected_owner: int, destination: Path,
) -> None:
    import qrcode
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            raise RuntimeError("Use a new empty local session for QR login")
        qr = await client.qr_login()
        code = qrcode.QRCode(border=2)
        code.add_data(qr.url)
        code.make(fit=True)
        print(
            "Scan ONLY on your own trusted workstation via Telegram "
            "Settings > Devices > Link Desktop Device.\n"
            "This grants full user-session access, not read-only permissions."
        )
        code.print_ascii(invert=True)
        try:
            await qr.wait(timeout=120)
        except errors.SessionPasswordNeededError:
            password = getpass.getpass(
                "Telegram 2FA password (entered locally only): "
            )
            try:
                await client.sign_in(password=password)
            finally:
                del password
        me = await client.get_me()
        if me is None or me.bot or me.id != expected_owner:
            raise RuntimeError("The authenticated account is not the expected owner")
        session = StringSession.save(client.session)
        if not session:
            raise RuntimeError("Telegram did not produce an authorized session")
        # Never overwrite an existing credential or print it to stdout/logs.
        fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="ascii") as file:
            file.write(session + "\n")
        print(
            "Authorized session saved privately to the requested path. "
            "Keep it OUT of Git, logs, chat, backups and public servers."
        )
    finally:
        await client.disconnect()


def main() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("Use a private interactive terminal only")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-id", type=int, required=True)
    parser.add_argument("--api-hash-file", type=Path, required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--out-file", type=Path, required=True)
    args = parser.parse_args()
    if args.api_id <= 0 or args.owner_id <= 0 or args.out_file.exists():
        raise SystemExit("API ID and owner ID must be positive; output must be new")
    api_hash = args.api_hash_file.read_text(encoding="ascii").strip()
    if len(api_hash) < 24:
        raise SystemExit("Invalid API hash secret")
    asyncio.run(_create_session(
        api_id=args.api_id, api_hash=api_hash,
        expected_owner=args.owner_id, destination=args.out_file,
    ))


if __name__ == "__main__":
    main()
