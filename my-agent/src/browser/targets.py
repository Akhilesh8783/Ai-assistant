"""The single ``target`` grammar shared by every action tool.

A voice model copies one short string from ``inspect_page`` instead of
reasoning about three optional locator parameters. Refs are preferred; the
prefixed locators are the escape hatch for elements that never appear in a
snapshot.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import AmbiguousTarget, InvalidArguments, StaleRef

if TYPE_CHECKING:
    from playwright.async_api import Frame, Locator, Page

    from .session import BrowserSession


_REF_RE = re.compile(r"^(?:f(?P<frame>\d+):)?e(?P<ref>\d+)$", re.IGNORECASE)
_ROLE_NAME_RE = re.compile(r"\s+name\s*=\s*(?P<name>.+)$", re.IGNORECASE | re.DOTALL)
_KEYS = {"role", "text", "label", "placeholder", "testid", "css", "xpath"}

GRAMMAR = (
    "Use e12 or f2:e12 from inspect_page, or role=button name=Submit, text=Sign in, "
    "label=Email, placeholder=Search, testid=login, css=#main button, xpath=//button[1]."
)


@dataclass(frozen=True, slots=True)
class Target:
    kind: str
    value: str
    name: str | None = None
    frame_index: int = 0
    raw: str = ""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_target(raw: str) -> Target:
    text = (raw or "").strip()
    if not text:
        raise InvalidArguments("I need a target to act on.", hint=GRAMMAR)

    match = _REF_RE.match(text)
    if match:
        index = int(match.group("frame") or 0)
        return Target(
            kind="ref",
            value=f"e{int(match.group('ref'))}",
            frame_index=index,
            raw=text,
        )

    if "=" not in text:
        raise InvalidArguments(
            f"{text!r} is not a target I understand.", hint=GRAMMAR
        )

    key, _, remainder = text.partition("=")
    key = key.strip().lower()
    value = remainder.strip()
    if key not in _KEYS or not value:
        raise InvalidArguments(
            f"The target prefix {key!r} is not supported.", hint=GRAMMAR
        )

    if key == "role":
        name = None
        name_match = _ROLE_NAME_RE.search(value)
        if name_match:
            name = _unquote(name_match.group("name"))
            value = value[: name_match.start()].strip()
        if not value:
            raise InvalidArguments("role= needs a role name, such as role=button.", hint=GRAMMAR)
        return Target(kind="role", value=_unquote(value), name=name, raw=text)

    return Target(kind=key, value=_unquote(value), raw=text)


def format_target(target: Target) -> str:
    if target.kind == "ref":
        return target.raw or target.value
    if target.kind == "role" and target.name:
        return f'role={target.value} name="{target.name}"'
    return f"{target.kind}={target.value}"


async def _wait_attached(locator: Locator, timeout_ms: int) -> int:
    try:
        await locator.first.wait_for(state="attached", timeout=timeout_ms)
    except Exception:
        return await locator.count()
    return await locator.count()


async def _describe_candidate(locator: Locator) -> str:
    try:
        info: dict[str, Any] = await locator.evaluate(
            """el => ({
                tag: (el.tagName || '').toLowerCase(),
                role: el.getAttribute('role') || '',
                name: (el.getAttribute('aria-label') || el.innerText || el.textContent || '')
                    .trim().replace(/\\s+/g, ' ').slice(0, 60),
                ref: el.getAttribute('data-jarvis-ref') || '',
            })"""
        )
    except Exception:
        return "an element"
    bits = [info.get("tag") or "element"]
    if info.get("role"):
        bits.append(f'role={info["role"]}')
    if info.get("name"):
        bits.append(f'"{info["name"]}"')
    if info.get("ref"):
        bits.append(f'[{info["ref"]}]')
    return " ".join(bits)


def _locator_for(frame: Frame, target: Target) -> Locator:
    if target.kind == "role":
        return frame.get_by_role(target.value, name=target.name)
    if target.kind == "text":
        return frame.get_by_text(target.value)
    if target.kind == "label":
        return frame.get_by_label(target.value)
    if target.kind == "placeholder":
        return frame.get_by_placeholder(target.value)
    if target.kind == "testid":
        return frame.get_by_test_id(target.value)
    if target.kind == "css":
        return frame.locator(target.value)
    if target.kind == "xpath":
        expression = target.value
        if not expression.lower().startswith(("xpath=", "/", "(")):
            expression = f"xpath={expression}"
        return frame.locator(expression)
    raise InvalidArguments(f"Unsupported target kind {target.kind!r}.", hint=GRAMMAR)


async def resolve_target(
    session: BrowserSession, target: Target
) -> tuple[Frame, Locator]:
    """Resolve a parsed target to a frame and a single-element locator."""
    page: Page | None = session.page
    if page is None:
        from .errors import NotStarted

        raise NotStarted(
            "No page is open yet.", hint="Call open_url first."
        )

    if target.kind == "ref":
        frames = page.frames
        if target.frame_index >= len(frames):
            raise StaleRef(
                "That reference belongs to a frame that is no longer on the page.",
                hint="Call inspect_page again.",
            )
        frame = frames[target.frame_index]
        locator = frame.locator(f'[data-jarvis-ref="{target.value}"]')
        if await locator.count() == 0:
            raise StaleRef(
                f"The reference {target.raw or target.value} is no longer on the page.",
                hint="Call inspect_page again to get current references.",
            )
        return frame, locator.first

    frame = page.main_frame
    locator = _locator_for(frame, target)
    try:
        count = await _wait_attached(locator, session.config.action_timeout_ms)
    except Exception as exc:
        raise InvalidArguments(
            f"{target.raw!r} is not a valid locator: {exc}"
        ) from exc

    if count == 0:
        from .errors import TargetNotFound

        raise TargetNotFound(
            f"Nothing on the page matches {target.raw!r}.",
            hint="Call inspect_page to see what is actually there.",
        )
    if count > 1:
        shown = []
        for i in range(min(count, 5)):
            shown.append(await _describe_candidate(locator.nth(i)))
        listing = "; ".join(shown)
        raise AmbiguousTarget(
            f"{target.raw!r} matches {count} elements: {listing}.",
            hint="Use a reference from inspect_page instead.",
        )
    return frame, locator.first


async def target_fingerprint(locator: Locator) -> str:
    """Stable-ish identity for an element, used to invalidate approvals."""
    try:
        info: dict[str, Any] = await locator.evaluate(
            """el => ({
                tag: (el.tagName || '').toLowerCase(),
                role: el.getAttribute('role') || '',
                name: (
                    el.getAttribute('aria-label') ||
                    el.innerText || el.textContent || ''
                ).trim().replace(/\\s+/g, ' ').slice(0, 120),
                type: el.getAttribute('type') || '',
                href: el.getAttribute('href') || '',
                ref: el.getAttribute('data-jarvis-ref') || '',
            })"""
        )
    except Exception:
        return ""
    payload = "|".join(str(info.get(key, "")) for key in sorted(info))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def frame_for_index(page: Page, index: int) -> Frame:
    frames = page.frames
    if index < 0 or index >= len(frames):
        raise InvalidArguments(f"Frame {index} does not exist on this page.")
    return frames[index]
