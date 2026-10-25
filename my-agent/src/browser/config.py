"""Configuration for the browser subsystem.

Every value comes from the environment so the same code runs locally (headed,
persistent profile) and in LiveKit Cloud (headless, ephemeral) without branches.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Mode = Literal["persistent", "cdp", "ephemeral"]
Toolset = Literal["full", "core"]

APPROVAL_TTL_S = 120.0
DIALOG_GRACE_S = 8.0

_TRUE = {"1", "true", "yes", "on"}


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in _TRUE


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_size(name: str, default: tuple[int, int]) -> tuple[int, int]:
    raw = _env(name)
    if not raw:
        return default
    try:
        width, height = raw.lower().split("x", 1)
        return int(width), int(height)
    except (ValueError, TypeError):
        return default


def _env_geo(name: str) -> tuple[float, float] | None:
    raw = _env(name)
    if not raw:
        return None
    try:
        lat, lng = raw.split(",", 1)
        return float(lat), float(lng)
    except (ValueError, TypeError):
        return None


def _default_user_data_dir() -> Path:
    return Path.home() / ".jarvis" / "chrome-profile"


def _default_download_dir() -> Path:
    return Path(tempfile.gettempdir()) / "jarvis-browser"


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    mode: Mode = "ephemeral"
    headless: bool = True
    channel: str | None = None
    cdp_url: str | None = None
    user_data_dir: Path | None = None
    state_enc: str | None = None
    state_key: str | None = None

    toolset: Toolset = "full"
    max_chars: int = 2400
    upload_dir: Path | None = None
    download_dir: Path | None = None
    allow_private: bool = False
    no_sandbox: bool = False
    http_user: str | None = None
    http_pass: str | None = None

    timeout_ms: int = 15_000
    action_timeout_ms: int = 8_000
    inspect_timeout_ms: int = 5_000
    script_timeout_ms: int = 8_000

    stream_enabled: bool = True
    stream_fps: int = 1
    stream_size: tuple[int, int] = (1280, 800)

    viewport: tuple[int, int] = (1280, 800)
    locale: str = "en-GB"
    timezone: str = "Europe/London"
    device: str | None = None
    geolocation: tuple[float, float] | None = None

    @classmethod
    def from_env(cls) -> BrowserConfig:
        mode_raw = _env("BROWSER_MODE", "ephemeral").lower()
        mode: Mode = mode_raw if mode_raw in ("persistent", "cdp", "ephemeral") else "ephemeral"  # type: ignore[assignment]

        toolset_raw = _env("BROWSER_TOOLSET", "full").lower()
        toolset: Toolset = "core" if toolset_raw == "core" else "full"

        user_data_raw = _env("BROWSER_USER_DATA_DIR")
        user_data_dir = Path(user_data_raw).expanduser() if user_data_raw else None
        if mode == "persistent" and user_data_dir is None:
            user_data_dir = _default_user_data_dir()

        upload_raw = _env("BROWSER_UPLOAD_DIR")
        download_raw = _env("BROWSER_DOWNLOAD_DIR")

        return cls(
            mode=mode,
            headless=_env_bool("BROWSER_HEADLESS", mode != "persistent"),
            channel=_env("BROWSER_CHANNEL") or None,
            cdp_url=_env("BROWSER_CDP_URL") or None,
            user_data_dir=user_data_dir,
            state_enc=_env("BROWSER_STATE_ENC") or None,
            state_key=_env("BROWSER_STATE_KEY") or None,
            toolset=toolset,
            max_chars=max(400, _env_int("BROWSER_MAX_CHARS", 2400)),
            upload_dir=Path(upload_raw).expanduser() if upload_raw else None,
            download_dir=Path(download_raw).expanduser() if download_raw else _default_download_dir(),
            allow_private=_env_bool("BROWSER_ALLOW_PRIVATE", False),
            no_sandbox=_env_bool("BROWSER_NO_SANDBOX", False),
            http_user=_env("BROWSER_HTTP_USER") or None,
            http_pass=_env("BROWSER_HTTP_PASS") or None,
            timeout_ms=_env_int("BROWSER_TIMEOUT_MS", 15_000),
            action_timeout_ms=_env_int("BROWSER_ACTION_TIMEOUT_MS", 8_000),
            inspect_timeout_ms=_env_int("BROWSER_INSPECT_TIMEOUT_MS", 5_000),
            script_timeout_ms=_env_int("BROWSER_SCRIPT_TIMEOUT_MS", 8_000),
            stream_enabled=_env_bool("BROWSER_STREAM", True),
            stream_fps=max(1, min(5, _env_int("BROWSER_STREAM_FPS", 1))),
            stream_size=_env_size("BROWSER_STREAM_SIZE", (1280, 800)),
            viewport=_env_size("BROWSER_VIEWPORT", (1280, 800)),
            locale=_env("BROWSER_LOCALE", "en-GB"),
            timezone=_env("BROWSER_TIMEZONE", "Europe/London"),
            device=_env("BROWSER_DEVICE") or None,
            geolocation=_env_geo("BROWSER_GEOLOCATION"),
        )
