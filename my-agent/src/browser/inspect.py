"""Reference-based page inspection.

``inspect_page`` injects ``data-jarvis-ref`` attributes and returns a compact
list of interactive elements. Refs are cheap for a model to copy and unambiguous
for Playwright to resolve, which removes most of the flakiness of asking a voice
model to invent CSS selectors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import InvalidArguments, TimeoutFailure

if TYPE_CHECKING:
    from playwright.async_api import Page

    from .session import BrowserSession

logger = logging.getLogger("browser.inspect")

_INSPECT_CAP = 3_000
_PER_FRAME_ITEMS = 24
_MAX_SCAN = 2_000


INIT_SCRIPT = r"""
(() => {
  if (window.__jarvisRev !== undefined) return;
  window.__jarvisRev = 0;
  let scheduled = false;
  const bump = () => {
    if (scheduled) return;
    scheduled = true;
    const schedule = window.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
    schedule(() => { scheduled = false; window.__jarvisRev += 1; });
  };
  const options = {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['class', 'style', 'hidden', 'disabled', 'value', 'type', 'href', 'src', 'aria-expanded', 'aria-hidden'],
  };
  const attach = () => {
    if (document.documentElement) {
      try { new MutationObserver(bump).observe(document.documentElement, options); } catch (e) {}
    }
  };
  if (document.documentElement) attach();
  else document.addEventListener('DOMContentLoaded', attach, { once: true });
})();
"""


_PROBE_JS = r"""
() => ({
  url: location.href,
  title: document.title,
  rev: (typeof window.__jarvisRev === 'number') ? window.__jarvisRev : -1,
})
"""


_SNAPSHOT_JS = r"""
(arg) => {
  const maxItems = arg.maxItems;
  const offset = arg.offset;
  const SEL = 'a[href],button,input,select,textarea,summary,[role],[contenteditable="true"],[onclick]';
  const MAX_SCAN = arg.maxScan;

  function* allRoots(root) {
    const start = root || document;
    yield start;
    const walker = document.createTreeWalker(start, NodeFilter.SHOW_ELEMENT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.shadowRoot) yield* allRoots(node.shadowRoot);
    }
  }

  for (const root of allRoots(document)) {
    root.querySelectorAll('[data-jarvis-ref]').forEach((el) => el.removeAttribute('data-jarvis-ref'));
  }

  function textOf(el) {
    return (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
  }

  function visible(el) {
    const style = window.getComputedStyle(el);
    if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
    const rect = el.getBoundingClientRect();
    return !(rect.width === 0 && rect.height === 0);
  }

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.trim().split(/\s+/)[0];
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'number') return 'spinbutton';
      if (type === 'search') return 'searchbox';
      if (type === 'button' || type === 'submit' || type === 'reset' || type === 'file') return 'button';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'generic';
  }

  function nameOf(el) {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();

    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const rootNode = el.getRootNode();
      const scope = (rootNode && rootNode.getElementById) ? rootNode : document;
      const parts = labelledby.split(/\s+/).map((id) => {
        const node = scope.getElementById(id);
        return node ? textOf(node) : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }

    if (el.id) {
      const rootNode = el.getRootNode();
      const scope = (rootNode && rootNode.querySelectorAll) ? rootNode : document;
      const label = Array.from(scope.querySelectorAll('label')).find((item) => item.htmlFor === el.id);
      if (label) {
        const value = textOf(label);
        if (value) return value;
      }
    }

    const wrapping = el.closest ? el.closest('label') : null;
    if (wrapping) {
      const value = textOf(wrapping);
      if (value) return value;
    }

    if (el.tagName === 'INPUT') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'button' || type === 'submit' || type === 'reset') {
        if (el.value) return String(el.value);
      }
    }
    if (el.tagName === 'IMG' && el.alt) return el.alt;

    const inner = textOf(el);
    if (inner) return inner;
    return el.getAttribute('placeholder') || el.getAttribute('title') || '';
  }

  const elements = [];
  function collect(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let el;
    while ((el = walker.nextNode())) {
      if (elements.length >= MAX_SCAN) return;
      try {
        if (el.matches(SEL) && visible(el)) elements.push(el);
      } catch (e) {}
      if (el.shadowRoot) collect(el.shadowRoot);
    }
  }
  collect(document);

  elements.forEach((el, index) => el.setAttribute('data-jarvis-ref', 'e' + (index + 1)));

  const shown = elements.slice(offset, offset + maxItems).map((el) => {
    const role = roleOf(el);
    const label = nameOf(el).replace(/\s+/g, ' ').slice(0, 70);
    let extra = '';
    if (el.disabled) extra += ' [disabled]';
    if (el.tagName === 'INPUT') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox' || type === 'radio') {
        extra += el.checked ? ' (checked)' : ' (unchecked)';
      } else if (type !== 'password') {
        const value = String(el.value || '');
        extra += value ? ' (value: ' + value.slice(0, 30) + ')' : ' (empty)';
      }
    }
    if (el.tagName === 'A' && el.getAttribute('href')) {
      extra += ' -> ' + el.getAttribute('href').slice(0, 80);
    }
    return { ref: el.getAttribute('data-jarvis-ref') || '', role, name: label, extra };
  });

  const headings = Array.from(document.querySelectorAll('h1, h2, h3'))
    .map((el) => (el.innerText || '').trim().replace(/\s+/g, ' '))
    .filter(Boolean)
    .slice(0, 8);

  const scrollable = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
  return {
    url: location.href,
    title: document.title,
    rev: (typeof window.__jarvisRev === 'number') ? window.__jarvisRev : -1,
    scrollPct: Math.min(100, Math.max(0, Math.round((window.scrollY / scrollable) * 100))),
    total: elements.length,
    headings,
    items: shown,
  };
}
"""


@dataclass(slots=True)
class Snapshot:
    page_id: int
    url: str
    rev: int
    frame_count: int
    text: str


async def ensure_init_script(page: Page) -> None:
    try:
        await page.add_init_script(INIT_SCRIPT)
    except Exception:  # pragma: no cover - a closed page is handled elsewhere
        logger.debug("could not install the revision observer", exc_info=True)
    try:
        await page.evaluate(INIT_SCRIPT)
    except Exception:
        logger.debug("could not run the revision observer now", exc_info=True)


async def probe(page: Page) -> dict:
    try:
        data = await page.evaluate(_PROBE_JS)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _frame_block(
    session: BrowserSession, page: Page, index: int, offset: int
) -> tuple[list[str], int]:
    frames = page.frames
    frame = frames[index]
    if frame.url in ("", "about:blank") and index != 0:
        return [], 0

    try:
        data = await frame.evaluate(
            _SNAPSHOT_JS,
            {"maxItems": _PER_FRAME_ITEMS, "offset": offset, "maxScan": _MAX_SCAN},
        )
    except Exception:
        logger.debug("frame %s could not be inspected", index, exc_info=True)
        return [], 0

    if not isinstance(data, dict):
        return [], 0

    total = int(data.get("total") or 0)
    lines: list[str] = []
    prefix = "" if index == 0 else f"f{index}:"
    if index != 0:
        lines.append(f"frame {index}: {frame.url}")

    for item in data.get("items") or []:
        ref = f"{prefix}{item.get('ref', '')}"
        role = item.get("role") or "element"
        name = item.get("name") or ""
        extra = item.get("extra") or ""
        label = f'"{name}"' if name else ""
        lines.append(f"[{ref}] {role} {label}{extra}".rstrip())

    if index == 0:
        headings = " | ".join(data.get("headings") or [])
        if headings:
            lines.append(f"headings: {headings}")

    return lines, total


async def snapshot(
    session: BrowserSession, *, offset: int = 0, selector: str | None = None
) -> str:
    page = session.page
    if page is None:
        from .errors import NotStarted

        raise NotStarted("No page is open yet.", hint="Call open_url first.")

    if offset < 0:
        raise InvalidArguments("offset cannot be negative.")
    if selector:
        # A selector-scoped snapshot is niche; use read_page for a region instead.
        raise InvalidArguments(
            "inspect_page does not take a selector. Use read_page(selector=...) for a region."
        )

    info = await probe(page)
    url = str(info.get("url") or page.url)
    rev = int(info.get("rev") if isinstance(info.get("rev"), int) else -1)
    frame_count = len(page.frames)
    page_id = id(page)

    cached = session.snapshot_cache
    if (
        offset == 0
        and cached is not None
        and cached.url == url
        and cached.rev == rev
        and cached.rev >= 0
        and cached.frame_count == frame_count
        and cached.page_id == page_id
    ):
        return (
            "The page has not changed since the last inspect_page. "
            "The references you already have are still valid."
        )

    try:
        import asyncio

        blocks = await asyncio.wait_for(
            asyncio.gather(
                *(_frame_block(session, page, i, offset) for i in range(frame_count))
            ),
            timeout=max(1.0, session.config.inspect_timeout_ms / 1000),
        )
    except TimeoutError as exc:
        raise TimeoutFailure(
            "The page took too long to inspect. It may still be loading."
        ) from exc

    lines: list[str] = []
    if not offset:
        lines.append(f"url: {url}")
        if info.get("title"):
            lines.append(f"title: {info['title']}")
    total_elements = 0
    for block, total in blocks:
        total_elements += total
        if block:
            if lines and not offset:
                lines.append("")
            lines.extend(block)

    shown_total = sum(len(block) for block, _ in blocks)
    if not offset:
        scroll = info.get("scrollPct")
        if isinstance(scroll, int):
            lines.insert(
                2,
                f"scroll: {scroll}% of page, {total_elements} interactive elements "
                f"(showing {shown_total})",
            )

    body = "\n".join(lines).strip()
    if not body or (offset and not body):
        body = "No interactive elements were found. Use read_page for the text."

    if len(body) > _INSPECT_CAP:
        body = body[:_INSPECT_CAP].rstrip() + "\n[truncated]"

    if offset:
        body += "\n\ntip: call inspect_page() with no offset to see the rest."
    elif total_elements > shown_total:
        body += (
            "\n\ntip: call inspect_page(offset="
            f"{offset + _PER_FRAME_ITEMS}) for more, or read_page for prose."
        )

    session.snapshot_cache = Snapshot(
        page_id=page_id, url=url, rev=rev, frame_count=frame_count, text=body
    )
    return body
