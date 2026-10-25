"""Encrypted ``storage_state`` export and import.

The live Chrome profile on your laptop cannot be shared with a Cloud job. The
supported bridge is a snapshot: export cookies and localStorage once, encrypt
them, store the ciphertext as a secret, and decrypt in memory at job start.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import BrowserConfig
from .errors import InvalidArguments


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def encrypt_state(state: dict[str, Any], key: str) -> str:
    payload = json.dumps(state, separators=(",", ":")).encode("utf-8")
    token = Fernet(key.encode("ascii")).encrypt(payload)
    return base64.b64encode(token).decode("ascii")


def decrypt_state(token: str, key: str) -> dict[str, Any]:
    try:
        raw = base64.b64decode(token.encode("ascii"), validate=True)
        payload = Fernet(key.encode("ascii")).decrypt(raw)
        data = json.loads(payload.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise InvalidArguments(
            "The browser storage state could not be decrypted. "
            "Check BROWSER_STATE_KEY and BROWSER_STATE_ENC."
        ) from exc
    if not isinstance(data, dict):
        raise InvalidArguments("The browser storage state is not an object.")
    return data


def storage_state_from_env(cfg: BrowserConfig) -> dict[str, Any] | None:
    if not cfg.state_enc or not cfg.state_key:
        return None
    return decrypt_state(cfg.state_enc, cfg.state_key)


async def _export(profile: Path, out: Path, channel: str | None) -> None:
    from playwright.async_api import async_playwright

    if not profile.exists():
        raise SystemExit(f"Profile not found: {profile}")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=True,
            channel=channel or None,
        )
        try:
            state = await context.storage_state()
        finally:
            await context.close()

    token = encrypt_state(state, _read_or_create_key(out.with_suffix(".key")))
    out.write_text(token, encoding="ascii")
    print(f"Wrote {out} ({len(token)} bytes).")
    print(f"Key written to {out.with_suffix('.key')}; store it as BROWSER_STATE_KEY.")
    print("Store the contents of the state file as BROWSER_STATE_ENC.")


def _read_or_create_key(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="ascii").strip()
    key = generate_key()
    path.write_text(key, encoding="ascii")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis browser storage state")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="export an encrypted storage state")
    export.add_argument("--profile", required=True, type=Path)
    export.add_argument("--out", required=True, type=Path)
    export.add_argument("--channel", default=None)

    key = sub.add_parser("keygen", help="print a new Fernet key")
    key.add_argument("--out", type=Path, default=None)

    args = parser.parse_args(argv)

    if args.command == "keygen":
        value = generate_key()
        if args.out:
            args.out.write_text(value, encoding="ascii")
            print(f"Wrote {args.out}.")
        else:
            print(value)
        return 0

    asyncio.run(_export(args.profile.expanduser(), args.out, args.channel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
