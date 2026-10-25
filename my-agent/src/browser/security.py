"""URL policy, DNS checks and path guards.

The browser runs inside the agent's network. Without a policy, a page or a
prompt-injected instruction could reach ``localhost``, cloud metadata
endpoints, or private RFC1918 hosts. Everything here fails closed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import BrowserConfig
from .errors import InvalidArguments, PolicyViolation

logger = logging.getLogger("browser.security")

ALLOWED_SCHEMES = {"http", "https"}
BENIGN_SCHEMES = {"data", "blob", "about", "chrome", "chrome-extension", "devtools"}

_DNS_TTL_S = 60.0
_dns_cache: dict[str, tuple[float, tuple[str, ...]]] = {}


def _is_blocked_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _resolve(host: str) -> tuple[str, ...]:
    now = time.monotonic()
    cached = _dns_cache.get(host)
    if cached and now - cached[0] < _DNS_TTL_S:
        return cached[1]

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except OSError as exc:
        raise PolicyViolation(
            f"I could not resolve the host {host!r}.",
            hint="Check the domain name and try again.",
        ) from exc

    addresses = tuple({info[4][0] for info in infos})
    _dns_cache[host] = (now, addresses)
    return addresses


async def check_url(url: str, cfg: BrowserConfig, *, resolve: bool = True) -> None:
    """Raise ``PolicyViolation`` unless ``url`` is a public http(s) URL."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()

    if scheme not in ALLOWED_SCHEMES:
        raise PolicyViolation(
            f"I can only open http and https addresses, not {scheme or 'that'} ones.",
            hint="Give me a normal web address.",
        )

    host = parts.hostname
    if not host:
        raise PolicyViolation(f"{url!r} has no host name.")

    if cfg.allow_private:
        return

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _is_blocked_ip(str(literal)):
            raise PolicyViolation(
                f"I am not allowed to open the private address {host}.",
                hint="Set BROWSER_ALLOW_PRIVATE=1 to permit local addresses.",
            )
        return

    if not resolve:
        return

    for address in await _resolve(host):
        if _is_blocked_ip(address):
            raise PolicyViolation(
                f"{host} resolves to a private address, so I will not open it.",
                hint="Set BROWSER_ALLOW_PRIVATE=1 to permit local addresses.",
            )


async def guard_request(route, cfg: BrowserConfig) -> None:
    """Check one subresource request and abort it if it targets a private host.

    Navigation is checked separately, but a public page can still embed an
    ``<img src="http://169.254.169.254/...">``. This closes that gap.
    """
    url = route.request.url
    scheme = urlsplit(url).scheme.lower()
    if scheme in BENIGN_SCHEMES:
        await route.continue_()
        return
    try:
        await check_url(url, cfg)
    except PolicyViolation:
        logger.warning("blocked subresource to a private host: %s", url[:200])
        await route.abort("blockedbyclient")
        return
    except Exception:  # never break a page because the guard failed
        logger.exception("route guard failed for %s", url[:200])
        await route.continue_()
        return
    await route.continue_()


def resolve_upload_path(raw: str, upload_dir: Path | None) -> Path:
    """Return the real path of an allowed upload, or raise."""
    if upload_dir is None:
        raise InvalidArguments(
            "File uploads are disabled because BROWSER_UPLOAD_DIR is not set."
        )

    root = upload_dir.expanduser().resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()

    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PolicyViolation(
            "I can only upload files from the configured upload directory."
        ) from exc

    if not path.is_file():
        raise InvalidArguments(f"The file {path.name!r} does not exist.")
    return path
