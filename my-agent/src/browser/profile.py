"""Create and sign in to the dedicated Jarvis Chrome profile.

Chrome 136+ ignores ``--remote-debugging-port`` when the default user-data
directory is used, so Jarvis keeps its own profile. Sign in once here and every
local job starts with those cookies.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

from .config import BrowserConfig


async def _init(config: BrowserConfig) -> None:
    from playwright.async_api import async_playwright

    profile = config.user_data_dir
    if profile is None:
        raise SystemExit("BROWSER_USER_DATA_DIR is not configured.")
    profile.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            channel=config.channel or None,
            viewport={"width": config.viewport[0], "height": config.viewport[1]},
            locale=config.locale,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("about:blank")

        print(f"Profile: {profile}")
        print("Sign in to the sites Jarvis needs, then close the browser window.")
        closed = asyncio.Event()
        context.on("close", lambda *_: closed.set())
        try:
            await closed.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            try:
                await context.close()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise the Jarvis Chrome profile")
    parser.add_argument("--profile", type=Path, default=None)
    args = parser.parse_args(argv)

    config = BrowserConfig.from_env()
    if args.profile:
        config = replace(config, user_data_dir=args.profile.expanduser())

    asyncio.run(_init(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
