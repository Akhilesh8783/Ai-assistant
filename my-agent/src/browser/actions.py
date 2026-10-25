"""Plain async browser verbs. No LiveKit imports: these are testable directly.

Every public action either executes immediately or, when consequential, stages
a ``PendingAction`` and returns a message telling the model to ask first.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .confirm import ApprovalStore, stage_pending
from .errors import (
    InvalidArguments,
    NavigationError,
    TimeoutFailure,
)
from .security import check_url
from .targets import (
    format_target,
    parse_target,
    resolve_target,
    target_fingerprint,
)

if TYPE_CHECKING:
    from livekit.agents import RunContext

    from .session import BrowserSession

logger = logging.getLogger("browser.actions")

_MODIFIERS = {"alt": "Alt", "control": "Control", "ctrl": "Control", "meta": "Meta", "shift": "Shift"}


def _normalise_modifiers(modifiers: list[str] | None) -> list[str]:
    if not modifiers:
        return []
    result = []
    for item in modifiers:
        key = _MODIFIERS.get(str(item).lower())
        if key is None:
            raise InvalidArguments(
                f"{item!r} is not a modifier. Use Alt, Control, Meta, or Shift."
            )
        result.append(key)
    return result


def _normalise_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise InvalidArguments("I need a web address to open.")
    if "://" not in url:
        url = "https://" + url
    return url


async def _delta(
    session: BrowserSession,
    *,
    label: str,
    before_url: str,
    pages_before: int,
    started: float,
) -> str:
    page = session.active_page()
    elapsed = int((time.monotonic() - started) * 1000)
    lines = [f"ok {label} in {elapsed}ms"]
    if page.url != before_url:
        lines.append(f"url: {page.url}")
    else:
        lines.append("url unchanged")

    if session.pending_dialog is not None and session.last_dialog:
        kind, message = session.last_dialog
        lines.append(f'dialog: {kind} "{message[:120]}" (pending; call handle_dialog)')

    pages = session.list_pages()
    if len(pages) > pages_before:
        try:
            title = await page.title()
        except Exception:
            title = ""
        lines.append(f'new tab: index {len(pages) - 1}, "{title[:60]}"')

    return "\n".join(lines)


async def open_url(session: BrowserSession, url: str, new_tab: bool = False) -> str:
    target_url = _normalise_url(url)
    await check_url(target_url, session.config)

    async with session.lock:
        page = await session.ensure_started()
        if new_tab:
            page = await session.new_tab()

        started = time.monotonic()
        status: int | None = None
        note = ""
        try:
            response = await page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=session.config.timeout_ms,
            )
            status = response.status if response is not None else None
        except PlaywrightTimeoutError:
            note = "\nnavigation was slow; the page may still be loading"
            if page.url in ("about:blank", ""):
                raise TimeoutFailure(
                    f"{target_url} did not load within "
                    f"{session.config.timeout_ms // 1000} seconds."
                ) from None
        except PlaywrightError as exc:
            message = str(exc).splitlines()[0][:200]
            raise NavigationError(f"I could not open {target_url}: {message}") from exc

        try:
            await page.wait_for_load_state("load", timeout=3_000)
        except Exception:
            pass

        await check_url(page.url, session.config)
        session.bump_epoch()

        try:
            title = (await page.title())[:120]
        except Exception:
            title = ""

        elapsed = int((time.monotonic() - started) * 1000)
        header = f"opened {page.url}"
        if status is not None:
            header += f" (HTTP {status})"
        lines = [header]
        if title:
            lines.append(f"title: {title}")
        if note:
            lines.append(note.strip())
        lines.append(f"in {elapsed}ms. Call inspect_page to see what you can click.")
        return "\n".join(lines)


async def inspect(session: BrowserSession, offset: int = 0) -> str:
    from .inspect import snapshot

    async with session.lock:
        return await snapshot(session, offset=offset)


async def read(session: BrowserSession, selector: str | None, offset: int, frame: int) -> str:
    from .reader import read_page

    async with session.lock:
        return await read_page(session, selector=selector, offset=offset, frame=frame)


async def do_click(
    session: BrowserSession,
    target: str,
    button: str = "left",
    click_count: int = 1,
    modifiers: list[str] | None = None,
    **_: Any,
) -> str:
    parsed = parse_target(target)
    keys = _normalise_modifiers(modifiers)
    if button not in ("left", "right", "middle"):
        raise InvalidArguments("button must be left, right, or middle.")

    page = session.active_page()
    before_url = page.url
    pages_before = len(session.list_pages())
    started = time.monotonic()
    _, locator = await resolve_target(session, parsed)
    await locator.click(
        button=button,
        click_count=max(1, min(3, click_count)),
        modifiers=keys,
        timeout=session.config.action_timeout_ms,
    )
    await _settle(page)
    label = f"clicked {format_target(parsed)}"
    if button != "left" or click_count > 1:
        label += f" ({button} x{click_count})"
    return await _delta(
        session,
        label=label,
        before_url=before_url,
        pages_before=pages_before,
        started=started,
    )


async def click(
    session: BrowserSession,
    ctx: RunContext,
    approvals: ApprovalStore,
    target: str,
    button: str = "left",
    click_count: int = 1,
    modifiers: list[str] | None = None,
    consequential: bool = False,
) -> str:
    parsed = parse_target(target)
    if not consequential:
        async with session.lock:
            return await do_click(session, target, button, click_count, modifiers)

    async with session.lock:
        _, locator = await resolve_target(session, parsed)
        fingerprint = await target_fingerprint(locator)
        page = session.active_page()
        summary = f'click {format_target(parsed)} on {page.url}'
        return stage_pending(
            approvals,
            ctx,
            session,
            tool="click",
            params={
                "target": target,
                "button": button,
                "click_count": click_count,
                "modifiers": modifiers or [],
            },
            summary=summary,
            fingerprint=fingerprint,
            frame_index=parsed.frame_index,
        )


async def do_type_text(
    session: BrowserSession,
    target: str,
    text: str,
    mode: str = "fill",
    submit: bool = False,
    **_: Any,
) -> str:
    parsed = parse_target(target)
    if mode not in ("fill", "type"):
        raise InvalidArguments("mode must be fill or type.")

    page = session.active_page()
    before_url = page.url
    pages_before = len(session.list_pages())
    started = time.monotonic()
    _, locator = await resolve_target(session, parsed)

    if mode == "fill":
        await locator.fill(text, timeout=session.config.action_timeout_ms)
    else:
        await locator.click(timeout=session.config.action_timeout_ms)
        await locator.press_sequentially(text, timeout=session.config.action_timeout_ms)

    if submit:
        await locator.press("Enter", timeout=session.config.action_timeout_ms)
        await _settle(page)

    # Never log or echo the typed text.
    logger.info("typed %d characters into %s", len(text), format_target(parsed))
    label = f"typed {len(text)} characters into {format_target(parsed)}"
    if submit:
        label += " and pressed Enter"
    return await _delta(
        session,
        label=label,
        before_url=before_url,
        pages_before=pages_before,
        started=started,
    )


async def type_text(
    session: BrowserSession,
    ctx: RunContext,
    approvals: ApprovalStore,
    target: str,
    text: str,
    mode: str = "fill",
    submit: bool = False,
    consequential: bool = False,
) -> str:
    parsed = parse_target(target)
    if not submit and not consequential:
        async with session.lock:
            return await do_type_text(session, target, text, mode, submit)

    async with session.lock:
        _, locator = await resolve_target(session, parsed)
        fingerprint = await target_fingerprint(locator)
        page = session.active_page()
        action = "type and press Enter in" if submit else "type into"
        summary = f"{action} {format_target(parsed)} on {page.url}"
        return stage_pending(
            approvals,
            ctx,
            session,
            tool="type_text",
            params={"target": target, "text": text, "mode": mode, "submit": submit},
            summary=summary,
            fingerprint=fingerprint,
            frame_index=parsed.frame_index,
        )


async def do_press_key(
    session: BrowserSession,
    key: str,
    modifiers: list[str] | None = None,
    target: str | None = None,
    **_: Any,
) -> str:
    keys = _normalise_modifiers(modifiers)
    page = session.active_page()
    before_url = page.url
    pages_before = len(session.list_pages())
    started = time.monotonic()

    if target:
        parsed = parse_target(target)
        _, locator = await resolve_target(session, parsed)
        await locator.press(key, modifiers=keys, timeout=session.config.action_timeout_ms)
    else:
        if keys:
            for modifier in keys:
                await page.keyboard.down(modifier)
            try:
                await page.keyboard.press(key)
            finally:
                for modifier in reversed(keys):
                    await page.keyboard.up(modifier)
        else:
            await page.keyboard.press(key)

    await _settle(page)
    return await _delta(
        session,
        label=f"pressed {key}",
        before_url=before_url,
        pages_before=pages_before,
        started=started,
    )


async def do_hover(session: BrowserSession, target: str, **_: Any) -> str:
    parsed = parse_target(target)
    page = session.active_page()
    before_url = page.url
    pages_before = len(session.list_pages())
    started = time.monotonic()
    _, locator = await resolve_target(session, parsed)
    await locator.hover(timeout=session.config.action_timeout_ms)
    await _settle(page, delay=0.2)
    return await _delta(
        session,
        label=f"hovered {format_target(parsed)}",
        before_url=before_url,
        pages_before=pages_before,
        started=started,
    )


async def do_scroll(
    session: BrowserSession,
    direction: str = "down",
    amount: int = 1,
    to_target: str | None = None,
    **_: Any,
) -> str:
    page = session.active_page()
    before_url = page.url
    pages_before = len(session.list_pages())
    started = time.monotonic()

    if to_target:
        parsed = parse_target(to_target)
        _, locator = await resolve_target(session, parsed)
        await locator.scroll_into_view_if_needed(timeout=session.config.action_timeout_ms)
        label = f"scrolled {format_target(parsed)} into view"
    else:
        direction = direction.lower()
        if direction not in ("up", "down", "top", "bottom"):
            raise InvalidArguments("direction must be up, down, top, or bottom.")
        amount = max(1, min(10, amount))
        if direction in ("top", "bottom"):
            await page.evaluate(
                "where => window.scrollTo({top: where === 'top' ? 0 : document.body.scrollHeight, behavior: 'instant'})",
                direction,
            )
            label = f"scrolled to the {direction}"
        else:
            height = page.viewport_size or {"height": 800}
            delta = int(height.get("height", 800) * 0.85 * amount)
            await page.mouse.wheel(0, delta if direction == "down" else -delta)
            label = f"scrolled {direction} {amount} screen(s)"

    await _settle(page, delay=0.25)
    return await _delta(
        session,
        label=label,
        before_url=before_url,
        pages_before=pages_before,
        started=started,
    )


async def do_navigate(session: BrowserSession, direction: str, **_: Any) -> str:
    page = session.active_page()
    before_url = page.url
    pages_before = len(session.list_pages())
    started = time.monotonic()
    direction = direction.lower()

    if direction == "back":
        await page.go_back(
            wait_until="domcontentloaded", timeout=session.config.action_timeout_ms
        )
    elif direction == "forward":
        await page.go_forward(
            wait_until="domcontentloaded", timeout=session.config.action_timeout_ms
        )
    elif direction == "reload":
        await page.reload(
            wait_until="domcontentloaded", timeout=session.config.action_timeout_ms
        )
    elif direction == "stop":
        await page.evaluate("window.stop()")
    else:
        raise InvalidArguments("direction must be back, forward, reload, or stop.")

    await _settle(page)
    return await _delta(
        session,
        label=f"browser {direction}",
        before_url=before_url,
        pages_before=pages_before,
        started=started,
    )


async def do_tabs(
    session: BrowserSession, action: str, index: int | None = None, **_: Any
) -> str:
    action = action.lower()
    pages = session.list_pages()

    if action == "list":
        if not pages:
            return "No tabs are open."
        active = session.page
        lines = []
        for i, page in enumerate(pages):
            try:
                title = (await page.title())[:70]
            except Exception:
                title = ""
            marker = " (active)" if page is active else ""
            lines.append(f'{i}: "{title}" {page.url}{marker}')
        return "tabs:\n" + "\n".join(lines)

    if action == "new":
        page = await session.new_tab()
        return f"opened tab {len(session.list_pages()) - 1} (about:blank). Call open_url next."

    if action == "switch":
        if index is None:
            raise InvalidArguments("switch needs an index.")
        session.set_active_page(index)
        page = session.active_page()
        return f'switched to tab {index}: "{await page.title()}" {page.url}'

    if action == "close":
        if index is None:
            raise InvalidArguments("close needs an index.")
        return await session.close_tab(index)

    raise InvalidArguments("action must be list, new, switch, or close.")


async def do_select_option(
    session: BrowserSession, target: str, values: list[str], **_: Any
) -> str:
    parsed = parse_target(target)
    if not values:
        raise InvalidArguments("values cannot be empty.")
    page = session.active_page()
    before_url = page.url
    pages_before = len(session.list_pages())
    started = time.monotonic()
    _, locator = await resolve_target(session, parsed)
    await locator.select_option(
        values, timeout=session.config.action_timeout_ms
    )
    await _settle(page)
    return await _delta(
        session,
        label=f"selected {', '.join(values)} in {format_target(parsed)}",
        before_url=before_url,
        pages_before=pages_before,
        started=started,
    )


async def do_set_checked(
    session: BrowserSession, target: str, checked: bool, **_: Any
) -> str:
    parsed = parse_target(target)
    page = session.active_page()
    before_url = page.url
    pages_before = len(session.list_pages())
    started = time.monotonic()
    _, locator = await resolve_target(session, parsed)
    await locator.set_checked(checked, timeout=session.config.action_timeout_ms)
    await _settle(page)
    verb = "checked" if checked else "unchecked"
    return await _delta(
        session,
        label=f"{verb} {format_target(parsed)}",
        before_url=before_url,
        pages_before=pages_before,
        started=started,
    )


async def do_drag(
    session: BrowserSession, target: str, to_target: str, **_: Any
) -> str:
    source = parse_target(target)
    destination = parse_target(to_target)
    page = session.active_page()
    before_url = page.url
    pages_before = len(session.list_pages())
    started = time.monotonic()
    _, source_locator = await resolve_target(session, source)
    _, destination_locator = await resolve_target(session, destination)
    await source_locator.drag_to(
        destination_locator, timeout=session.config.action_timeout_ms
    )
    await _settle(page)
    return await _delta(
        session,
        label=f"dragged {format_target(source)} to {format_target(destination)}",
        before_url=before_url,
        pages_before=pages_before,
        started=started,
    )


async def do_handle_dialog(
    session: BrowserSession, action: str, prompt_text: str | None = None, **_: Any
) -> str:
    return await session.handle_dialog(action, prompt_text)


async def _settle(page, delay: float = 0.15) -> None:
    import asyncio

    await asyncio.sleep(delay)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=1_000)
    except Exception:
        pass


async def locked(session: BrowserSession, fn, *args: Any, **kwargs: Any) -> str:
    """Run a verb under the session lock."""
    async with session.lock:
        return await fn(session, *args, **kwargs)


# ------------------------------------------------------------------ dispatch
_EXECUTORS = {
    "click": do_click,
    "type_text": do_type_text,
    "press_key": do_press_key,
    "hover": do_hover,
    "scroll": do_scroll,
    "navigate": do_navigate,
    "tabs": do_tabs,
    "select_option": do_select_option,
    "set_checked": do_set_checked,
    "drag": do_drag,
    "handle_dialog": do_handle_dialog,
}


async def execute_staged(session: BrowserSession, tool: str, params: dict[str, Any]) -> str:
    from . import advanced

    executor = _EXECUTORS.get(tool)
    if executor is not None:
        return await executor(session, **params)
    advanced_executor = advanced.EXECUTORS.get(tool)
    if advanced_executor is not None:
        return await advanced_executor(session, **params)
    raise InvalidArguments(f"I do not know how to execute the staged action {tool!r}.")
