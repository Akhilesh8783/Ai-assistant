"""Advanced Playwright surface: scripts, files, storage, network, emulation.

These tools exist for completeness. Anything that can change the page in a way
the user did not explicitly ask for is staged behind the same approval gate as
a consequential click.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .confirm import ApprovalStore, stage_pending
from .errors import (
    DownloadFailed,
    InvalidArguments,
    TimeoutFailure,
)
from .security import resolve_upload_path
from .targets import parse_target, resolve_target, target_fingerprint

if TYPE_CHECKING:
    from livekit.agents import RunContext

    from .session import BrowserSession

logger = logging.getLogger("browser.advanced")

_MAX_RESULT = 8_000
_MAX_TEXT = 2_000


def _cap(value: str, limit: int = _MAX_RESULT) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n[truncated at {limit} characters]"


def _json(value: Any, limit: int = _MAX_RESULT) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return _cap(text, limit)


async def _staged(
    session: BrowserSession,
    ctx: RunContext,
    approvals: ApprovalStore,
    *,
    tool: str,
    params: dict[str, Any],
    summary: str,
    fingerprint: str = "",
) -> str:
    async with session.lock:
        return stage_pending(
            approvals,
            ctx,
            session,
            tool=tool,
            params=params,
            summary=summary,
            fingerprint=fingerprint,
        )


# --------------------------------------------------------------------- script
async def do_run_browser_script(
    session: BrowserSession, script: str, frame: int = 0, **_: Any
) -> str:
    if not script or not script.strip():
        raise InvalidArguments("script cannot be empty.")
    if len(script) > 10_000:
        raise InvalidArguments("That script is too long; the limit is 10,000 characters.")

    page = session.active_page()
    frames = page.frames
    if frame < 0 or frame >= len(frames):
        raise InvalidArguments(f"Frame {frame} does not exist on this page.")

    try:
        result = await asyncio.wait_for(
            frames[frame].evaluate(script),
            timeout=session.config.script_timeout_ms / 1000,
        )
    except TimeoutError as exc:
        raise TimeoutFailure(
            "The script did not finish within the time limit. It may be looping."
        ) from exc

    return "script result:\n" + _json(result)


async def run_browser_script(
    session: BrowserSession,
    ctx: RunContext,
    approvals: ApprovalStore,
    script: str,
    frame: int = 0,
) -> str:
    return await _staged(
        session,
        ctx,
        approvals,
        tool="run_browser_script",
        params={"script": script, "frame": frame},
        summary=f"run a script on {session.active_page().url if session.page else 'the page'}",
    )


# ---------------------------------------------------------------------- files
async def do_browser_files(
    session: BrowserSession,
    action: str,
    target: str | None = None,
    paths: list[str] | None = None,
    name: str | None = None,
    full_page: bool = False,
    **_: Any,
) -> str:
    page = session.active_page()
    context = session.context
    action = action.lower()

    if action == "upload":
        if not target:
            raise InvalidArguments("upload needs a target, such as an input[type=file] ref.")
        if not paths:
            raise InvalidArguments("upload needs at least one path.")
        uploads = [resolve_upload_path(path, session.config.upload_dir) for path in paths]
        parsed = parse_target(target)
        _, locator = await resolve_target(session, parsed)
        await locator.set_input_files(
            [str(path) for path in uploads], timeout=session.config.action_timeout_ms
        )
        names = ", ".join(path.name for path in uploads)
        return f"uploaded {names} to {target}."

    if action == "download_latest":
        downloads = session.downloads
        if not downloads:
            raise DownloadFailed("Nothing has been downloaded in this session yet.")
        download = downloads[-1]
        filename = name or download.suggested_filename or "download"
        destination = Path(session.download_dir or ".") / _safe_name(filename)
        try:
            await download.save_as(str(destination))
        except Exception as exc:
            raise DownloadFailed(f"The download could not be saved: {exc}") from exc
        size = destination.stat().st_size if destination.exists() else 0
        preview = ""
        if size and size < 200_000:
            try:
                preview = destination.read_text(encoding="utf-8", errors="replace")[:_MAX_TEXT]
            except Exception:
                preview = ""
        text = f"saved {destination.name} ({size} bytes) to {destination}"
        if preview:
            text += f"\npreview:\n{preview}"
        return text

    if action == "screenshot":
        if context is None:
            raise InvalidArguments("No browser is running yet.")
        raw = await page.screenshot(
            full_page=full_page, type="png", animations="disabled", caret="hide"
        )
        destination = Path(session.download_dir or ".") / f"screenshot-{int(time.time())}.png"
        destination.write_bytes(raw)
        session.notify_capture()
        return f"captured a {'full page ' if full_page else ''}screenshot to {destination}."

    if action == "pdf":
        if not session.config.headless:
            raise InvalidArguments("PDF export only works in headless mode.")
        destination = Path(session.download_dir or ".") / f"page-{int(time.time())}.pdf"
        await page.pdf(path=str(destination))
        return f"saved a PDF to {destination}."

    if action == "trace_start":
        if context is None:
            raise InvalidArguments("No browser is running yet.")
        await context.tracing.start(screenshots=True, snapshots=True, sources=False)
        return "tracing started."

    if action == "trace_stop":
        if context is None:
            raise InvalidArguments("No browser is running yet.")
        destination = Path(session.download_dir or ".") / f"trace-{int(time.time())}.zip"
        await context.tracing.stop(path=str(destination))
        return f"saved the trace to {destination}."

    raise InvalidArguments(
        "action must be upload, download_latest, screenshot, pdf, trace_start, or trace_stop."
    )


def _safe_name(name: str) -> str:
    cleaned = Path(name).name
    return cleaned or "download"


# -------------------------------------------------------------------- storage
_STORAGE_JS = r"""
(kind, key) => {
  const store = kind === 'session' ? window.sessionStorage : window.localStorage;
  if (key) return store.getItem(key);
  const out = {};
  for (let i = 0; i < store.length; i += 1) {
    const name = store.key(i);
    if (name !== null) out[name] = store.getItem(name);
  }
  return out;
}
"""


async def do_browser_storage(
    session: BrowserSession,
    action: str,
    name: str | None = None,
    value: str | None = None,
    domain: str | None = None,
    url: str | None = None,
    include_values: bool = False,
    kind: str = "local",
    key: str | None = None,
    **_: Any,
) -> str:
    page = session.active_page()
    context = session.context
    if context is None:
        raise InvalidArguments("No browser is running yet.")
    action = action.lower()

    if action == "cookies":
        cookies = await context.cookies()
        rows = []
        for cookie in cookies[:80]:
            row = {
                "name": cookie.get("name"),
                "domain": cookie.get("domain"),
                "path": cookie.get("path"),
                "expires": cookie.get("expires"),
                "httpOnly": cookie.get("httpOnly"),
                "secure": cookie.get("secure"),
            }
            if include_values:
                row["value"] = cookie.get("value")
            rows.append(row)
        return _json(rows)

    if action == "set_cookie":
        if not name or value is None:
            raise InvalidArguments("set_cookie needs name and value.")
        payload: dict[str, Any] = {"name": name, "value": value, "path": "/"}
        if domain:
            payload["domain"] = domain
        elif url:
            payload["url"] = url
        else:
            payload["url"] = page.url
        await context.add_cookies([payload])
        return f"set cookie {name!r}."

    if action == "clear_cookies":
        await context.clear_cookies()
        return "cleared all cookies."

    if action in ("local_storage", "session_storage"):
        store_kind = "session" if action == "session_storage" else "local"
        data = await page.evaluate(_STORAGE_JS, [store_kind, key])
        return _json(data, _MAX_TEXT)

    if action == "clear_storage":
        if kind not in ("local", "session"):
            raise InvalidArguments("kind must be local or session.")
        await page.evaluate(
            "(kind) => (kind === 'session' ? window.sessionStorage : window.localStorage).clear()",
            kind,
        )
        return f"cleared {kind} storage."

    raise InvalidArguments(
        "action must be cookies, set_cookie, clear_cookies, local_storage, "
        "session_storage, or clear_storage."
    )


async def browser_storage(
    session: BrowserSession,
    ctx: RunContext,
    approvals: ApprovalStore,
    action: str,
    name: str | None = None,
    value: str | None = None,
    domain: str | None = None,
    url: str | None = None,
    include_values: bool = False,
    kind: str = "local",
    key: str | None = None,
) -> str:
    if action in ("clear_cookies", "clear_storage"):
        return await _staged(
            session,
            ctx,
            approvals,
            tool="browser_storage",
            params={
                "action": action,
                "name": name,
                "value": value,
                "domain": domain,
                "url": url,
                "include_values": include_values,
                "kind": kind,
                "key": key,
            },
            summary=f"{action.replace('_', ' ')} on {session.page.url if session.page else 'the page'}",
        )
    async with session.lock:
        return await do_browser_storage(
            session,
            action,
            name=name,
            value=value,
            domain=domain,
            url=url,
            include_values=include_values,
            kind=kind,
            key=key,
        )


# -------------------------------------------------------------------- network
async def do_browser_network(
    session: BrowserSession,
    action: str,
    url_contains: str | None = None,
    timeout_s: int = 15,
    pattern: str | None = None,
    limit: int = 20,
    status: int = 200,
    body: str = "{}",
    headers: dict[str, str] | None = None,
    offline: bool | None = None,
    **_: Any,
) -> str:
    page = session.active_page()
    action = action.lower()

    if action == "wait_for_response":
        if not url_contains:
            raise InvalidArguments("wait_for_response needs url_contains.")
        seconds = max(1, min(15, timeout_s))
        try:
            response = await page.wait_for_response(
                lambda r: url_contains in r.url, timeout=seconds * 1000
            )
        except Exception as exc:
            raise TimeoutFailure(
                f"No response containing {url_contains!r} arrived within {seconds} seconds."
            ) from exc
        text = ""
        try:
            text = (await response.text())[:_MAX_TEXT]
        except Exception:
            pass
        result = f"HTTP {response.status} {response.url}"
        if text:
            result += f"\n{text}"
        return result

    if action == "requests":
        rows = session.requests[-max(1, min(100, limit)) :]
        compact = [
            {
                "method": row["method"],
                "url": row["url"][:200],
                "status": row["status"],
                "type": row["type"],
                "ms": row["duration_ms"],
                "failed": row["failed"],
            }
            for row in reversed(rows)
        ]
        return _json(compact)

    if action == "block":
        if not pattern:
            raise InvalidArguments("block needs a pattern, such as doubleclick.net.")
        session.block_pattern(pattern)
        return f"blocking requests matching {pattern!r}."

    if action == "unblock":
        session.clear_blocked()
        return "cleared all request blocks."

    if action == "mock":
        if not pattern:
            raise InvalidArguments("mock needs a pattern.")
        session.add_mock(pattern, max(100, min(599, status)), body)
        return f"mocking requests matching {pattern!r} with HTTP {status}."

    if action == "clear_mocks":
        session.clear_mocks()
        return "cleared all mocks."

    if action == "headers":
        session._extra_headers = dict(headers or {})
        await session.apply_network_rules()
        return f"set {len(session._extra_headers)} extra header(s)."

    if action == "clear_headers":
        session._extra_headers = {}
        await session.apply_network_rules()
        return "cleared extra headers."

    if action == "offline":
        session.set_offline(bool(offline))
        await session.apply_network_rules()
        state = "offline" if session.offline else "online"
        return f"the browser is now {state}."

    raise InvalidArguments(
        "action must be wait_for_response, requests, block, unblock, mock, "
        "clear_mocks, headers, clear_headers, or offline."
    )


async def browser_network(
    session: BrowserSession,
    ctx: RunContext,
    approvals: ApprovalStore,
    action: str,
    url_contains: str | None = None,
    timeout_s: int = 15,
    pattern: str | None = None,
    limit: int = 20,
    status: int = 200,
    body: str = "{}",
    headers: dict[str, str] | None = None,
    offline: bool | None = None,
) -> str:
    if action == "mock":
        return await _staged(
            session,
            ctx,
            approvals,
            tool="browser_network",
            params={
                "action": action,
                "url_contains": url_contains,
                "timeout_s": timeout_s,
                "pattern": pattern,
                "limit": limit,
                "status": status,
                "body": body,
                "headers": headers,
                "offline": offline,
            },
            summary=f"mock requests matching {pattern!r}",
        )
    async with session.lock:
        return await do_browser_network(
            session,
            action,
            url_contains=url_contains,
            timeout_s=timeout_s,
            pattern=pattern,
            limit=limit,
            status=status,
            body=body,
            headers=headers,
            offline=offline,
        )


# ------------------------------------------------------------------ emulation
async def do_browser_emulate(
    session: BrowserSession,
    action: str,
    width: int | None = None,
    height: int | None = None,
    color_scheme: str | None = None,
    reduced_motion: str | None = None,
    permissions: list[str] | None = None,
    clock_action: str | None = None,
    ms: int = 0,
    device: str | None = None,
    locale: str | None = None,
    timezone: str | None = None,
    geolocation: str | None = None,
    user_agent: str | None = None,
    **_: Any,
) -> str:
    page = session.active_page()
    action = action.lower()

    if action == "viewport":
        if not width or not height:
            raise InvalidArguments("viewport needs width and height.")
        await page.set_viewport_size({"width": int(width), "height": int(height)})
        return f"set the viewport to {width}x{height}."

    if action == "media":
        await page.emulate_media(
            color_scheme=color_scheme or None,
            reduced_motion=reduced_motion or None,
        )
        return f"emulated media (colour scheme {color_scheme or 'default'})."

    if action == "permissions":
        context = session.context
        if context is None:
            raise InvalidArguments("No browser is running yet.")
        await context.grant_permissions(permissions or [])
        return f"granted {', '.join(permissions or []) or 'no permissions'}."

    if action == "clock":
        if not clock_action:
            raise InvalidArguments("clock needs clock_action: install, fast_forward, pause, or resume.")
        clock = getattr(page, "clock", None)
        if clock is None:
            raise InvalidArguments("This Playwright version does not expose the clock API.")
        if clock_action == "install":
            await clock.install()
        elif clock_action == "fast_forward":
            await clock.fast_forward(ms or 1000)
        elif clock_action == "pause":
            await clock.pause_at(None) if hasattr(clock, "pause_at") else await clock.pause_at(0)
        elif clock_action == "resume":
            await clock.resume()
        else:
            raise InvalidArguments("clock_action must be install, fast_forward, pause, or resume.")
        return f"clock {clock_action}."

    if action == "restart_context":
        return "restart_context must be confirmed before it runs."

    raise InvalidArguments(
        "action must be viewport, media, permissions, clock, or restart_context."
    )


async def do_restart_context(
    session: BrowserSession,
    device: str | None = None,
    locale: str | None = None,
    timezone: str | None = None,
    geolocation: str | None = None,
    user_agent: str | None = None,
    **_: Any,
) -> str:
    overrides: dict[str, Any] = {}
    if locale:
        overrides["locale"] = locale
    if timezone:
        overrides["timezone"] = timezone
    if device:
        overrides["device"] = device
    if user_agent:
        overrides["device"] = None

    session.config = replace(session.config, **overrides)
    session.bump_epoch()
    await session.restart()

    if user_agent:
        page = session.active_page()
        context = session.context
        if context is not None:
            await context.set_extra_http_headers({"User-Agent": user_agent})
        await page.reload(wait_until="domcontentloaded")

    return (
        "restarted the browser context with the new settings; "
        "the page state and tabs were reset."
    )


async def browser_emulate(
    session: BrowserSession,
    ctx: RunContext,
    approvals: ApprovalStore,
    action: str,
    width: int | None = None,
    height: int | None = None,
    color_scheme: str | None = None,
    reduced_motion: str | None = None,
    permissions: list[str] | None = None,
    clock_action: str | None = None,
    ms: int = 0,
    device: str | None = None,
    locale: str | None = None,
    timezone: str | None = None,
    geolocation: str | None = None,
    user_agent: str | None = None,
) -> str:
    if action == "restart_context":
        return await _staged(
            session,
            ctx,
            approvals,
            tool="restart_context",
            params={
                "device": device,
                "locale": locale,
                "timezone": timezone,
                "geolocation": geolocation,
                "user_agent": user_agent,
            },
            summary="restart the browser context, losing the current page state",
        )
    async with session.lock:
        return await do_browser_emulate(
            session,
            action,
            width=width,
            height=height,
            color_scheme=color_scheme,
            reduced_motion=reduced_motion,
            permissions=permissions,
            clock_action=clock_action,
            ms=ms,
            device=device,
            locale=locale,
            timezone=timezone,
            geolocation=geolocation,
            user_agent=user_agent,
        )


# ----------------------------------------------------------------------- wait
async def do_wait_for(
    session: BrowserSession,
    selector: str | None = None,
    text: str | None = None,
    url: str | None = None,
    load_state: str | None = None,
    function: str | None = None,
    response: str | None = None,
    timeout_s: int = 15,
    **_: Any,
) -> str:
    page = session.active_page()
    seconds = max(1, min(15, timeout_s))
    timeout = seconds * 1000
    chosen = [name for name, value in {
        "selector": selector,
        "text": text,
        "url": url,
        "load_state": load_state,
        "function": function,
        "response": response,
    }.items() if value]
    if len(chosen) != 1:
        raise InvalidArguments(
            "wait_for needs exactly one of selector, text, url, load_state, function, or response."
        )

    started = time.monotonic()
    try:
        if selector:
            await page.wait_for_selector(selector, timeout=timeout)
            what = f'selector {selector!r}'
        elif text:
            await page.get_by_text(text).first.wait_for(timeout=timeout)
            what = f"text {text!r}"
        elif url:
            await page.wait_for_url(url, timeout=timeout)
            what = f"url {url!r}"
        elif load_state:
            await page.wait_for_load_state(load_state, timeout=timeout)
            what = f"load state {load_state!r}"
        elif function:
            await page.wait_for_function(function, timeout=timeout)
            what = "the script condition"
        else:
            await page.wait_for_response(
                lambda r: response in r.url, timeout=timeout
            )
            what = f"a response containing {response!r}"
    except TimeoutError as exc:
        raise TimeoutFailure(f"{what} did not appear within {seconds} seconds.") from exc

    elapsed = int((time.monotonic() - started) * 1000)
    return f"waited {elapsed}ms for {what}."


async def wait_for(session: BrowserSession, **kwargs: Any) -> str:
    async with session.lock:
        return await do_wait_for(session, **kwargs)


# ---------------------------------------------------------------------- input
async def do_browser_input(
    session: BrowserSession,
    action: str,
    x: float | None = None,
    y: float | None = None,
    button: str = "left",
    dx: float | None = None,
    dy: float | None = None,
    text: str | None = None,
    **_: Any,
) -> str:
    page = session.active_page()
    action = action.lower()

    if action == "mouse_move":
        if x is None or y is None:
            raise InvalidArguments("mouse_move needs x and y.")
        await page.mouse.move(x, y)
        return f"moved the mouse to {x},{y}."

    if action == "mouse_down":
        await page.mouse.down(button=button)
        return f"pressed the {button} mouse button."

    if action == "mouse_up":
        await page.mouse.up(button=button)
        return f"released the {button} mouse button."

    if action == "wheel":
        await page.mouse.wheel(dx or 0, dy or 0)
        return f"scrolled by {dx or 0},{dy or 0}."

    if action == "tap":
        if x is None or y is None:
            raise InvalidArguments("tap needs x and y.")
        try:
            await page.touchscreen.tap(x, y)
        except Exception:
            await page.mouse.click(x, y)
        return f"tapped {x},{y}."

    if action == "type":
        if not text:
            raise InvalidArguments("type needs text.")
        await page.keyboard.type(text)
        return f"typed {len(text)} characters at the current focus."

    raise InvalidArguments("action must be mouse_move, mouse_down, mouse_up, wheel, tap, or type.")


async def browser_input(
    session: BrowserSession,
    ctx: RunContext,
    approvals: ApprovalStore,
    action: str,
    x: float | None = None,
    y: float | None = None,
    button: str = "left",
    dx: float | None = None,
    dy: float | None = None,
    text: str | None = None,
) -> str:
    if action in ("mouse_down", "tap"):
        return await _staged(
            session,
            ctx,
            approvals,
            tool="browser_input",
            params={
                "action": action,
                "x": x,
                "y": y,
                "button": button,
                "dx": dx,
                "dy": dy,
                "text": text,
            },
            summary=f"{action.replace('_', ' ')} at {x},{y} on {session.page.url if session.page else 'the page'}",
        )
    async with session.lock:
        return await do_browser_input(
            session, action, x=x, y=y, button=button, dx=dx, dy=dy, text=text
        )


EXECUTORS = {
    "run_browser_script": do_run_browser_script,
    "browser_files": do_browser_files,
    "browser_storage": do_browser_storage,
    "browser_network": do_browser_network,
    "browser_emulate": do_browser_emulate,
    "restart_context": do_restart_context,
    "wait_for": do_wait_for,
    "browser_input": do_browser_input,
}
