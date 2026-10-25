"""Browser lifecycle: one browser per job, one lock, lazy start.

Realtime models can emit parallel tool calls. A single lock serialises page
operations so two ``goto`` calls cannot interleave and corrupt state. Video
capture deliberately does not take this lock (see ``stream.py``).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Download,
    Page,
    Playwright,
    async_playwright,
)

from .config import DIALOG_GRACE_S, BrowserConfig
from .errors import BrowserCrashed, InvalidArguments, NotStarted
from .inspect import INIT_SCRIPT, Snapshot
from .security import guard_request
from .state import storage_state_from_env

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

logger = logging.getLogger("browser.session")


class BrowserSession:
    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._created_pages: set[Page] = set()

        self._closed = False
        self._started = False
        self._in_action = 0
        self._action_owner: asyncio.Task | None = None

        self.epoch = 0
        self.snapshot_cache: Snapshot | None = None

        self.pending_dialog: Dialog | None = None
        self.last_dialog: tuple[str, str] | None = None
        self._dialog_task: asyncio.Task[None] | None = None

        self._requests: deque[dict[str, Any]] = deque(maxlen=250)
        self._request_records: dict[int, dict[str, Any]] = {}
        self._downloads: deque[Download] = deque(maxlen=20)
        self._blocked: list[str] = []
        self._mocks: list[tuple[str, int, str]] = []
        self._extra_headers: dict[str, str] = {}
        self._offline = False

        self.work_dir: Path | None = None
        self.download_dir: Path | None = None
        self.capture_hooks: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ state
    @property
    def page(self) -> Page | None:
        return self._page

    @property
    def context(self) -> BrowserContext | None:
        return self._context

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def downloads(self) -> list[Download]:
        return list(self._downloads)

    @property
    def requests(self) -> list[dict[str, Any]]:
        return list(self._requests)

    def active_page(self) -> Page:
        if self._page is None:
            raise NotStarted("No page is open yet.", hint="Call open_url first.")
        return self._page

    def frame_url(self, index: int = 0) -> str:
        page = self._page
        if page is None:
            return ""
        frames = page.frames
        if 0 <= index < len(frames):
            return frames[index].url
        return ""

    def notify_capture(self) -> None:
        for hook in list(self.capture_hooks):
            try:
                hook()
            except Exception:  # pragma: no cover - a bad hook must not break actions
                logger.debug("capture hook failed", exc_info=True)

    @asynccontextmanager
    async def action(self) -> AsyncIterator[None]:
        """Mark a window in which popups belong to Jarvis (CDP mode)."""
        self._in_action += 1
        self._action_owner = asyncio.current_task()
        try:
            yield
        finally:
            self._in_action -= 1
            if self._in_action == 0:
                self._action_owner = None
            self.notify_capture()

    def bump_epoch(self) -> None:
        self.epoch += 1
        self.snapshot_cache = None

    # --------------------------------------------------------------- lifecycle
    async def ensure_started(self) -> Page:
        if self._page is not None and not self._closed:
            return self._page

        async with self._start_lock:
            if self._page is not None and not self._closed:
                return self._page

            self._closed = False
            self._prepare_work_dir()
            self._pw = await async_playwright().start()
            await self._launch()
            self._started = True
            self.notify_capture()
            return self.active_page()

    async def restart(self) -> Page:
        await self.close()
        self._closed = False
        return await self.ensure_started()

    async def _launch(self) -> None:
        assert self._pw is not None
        config = self.config
        args = ["--disable-blink-features=AutomationControlled"]
        if config.no_sandbox:
            args += ["--no-sandbox", "--disable-dev-shm-usage"]

        if config.mode == "persistent":
            await self._launch_persistent(args)
        elif config.mode == "cdp":
            await self._launch_cdp(args)
        else:
            await self._launch_ephemeral(args)

        context = self._context
        assert context is not None
        context.set_default_timeout(config.action_timeout_ms)
        context.set_default_navigation_timeout(config.timeout_ms)
        await context.add_init_script(INIT_SCRIPT)
        await context.route("**/*", self._route)
        context.on("page", self._on_new_page)
        context.on("close", self._on_context_close)

        for page in context.pages:
            self._install_page(page)

        if self._page is None:
            pages = context.pages
            self._page = pages[0] if pages else await context.new_page()
            self._created_pages.add(self._page)

    async def _launch_persistent(self, args: list[str]) -> None:
        assert self._pw is not None
        config = self.config
        profile = config.user_data_dir
        if profile is None:
            raise InvalidArguments("BROWSER_USER_DATA_DIR is required in persistent mode.")
        profile.mkdir(parents=True, exist_ok=True)

        logger.info("launching persistent Chromium profile at %s", profile)
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=config.headless,
            channel=config.channel or None,
            args=args,
            viewport={"width": config.viewport[0], "height": config.viewport[1]},
            locale=config.locale,
            timezone_id=config.timezone,
            accept_downloads=True,
            downloads_path=str(self.download_dir) if self.download_dir else None,
            **self._credentials_kwargs(),
        )

    async def _launch_ephemeral(self, args: list[str]) -> None:
        assert self._pw is not None
        config = self.config
        self._browser = await self._pw.chromium.launch(
            headless=config.headless,
            channel=config.channel or None,
            args=args,
        )
        kwargs = self._context_kwargs()
        self._context = await self._browser.new_context(**kwargs)
        self._page = await self._context.new_page()
        self._created_pages.add(self._page)

    async def _launch_cdp(self, args: list[str]) -> None:
        assert self._pw is not None
        config = self.config
        if not config.cdp_url:
            raise InvalidArguments("BROWSER_CDP_URL is required in cdp mode.")

        logger.info("connecting to an existing browser at %s", config.cdp_url)
        self._browser = await self._pw.chromium.connect_over_cdp(config.cdp_url)
        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
        else:
            self._context = await self._browser.new_context(**self._context_kwargs())

        self._page = await self._context.new_page()
        self._created_pages.add(self._page)

    def _context_kwargs(self) -> dict[str, Any]:
        config = self.config
        kwargs: dict[str, Any] = {
            "viewport": {"width": config.viewport[0], "height": config.viewport[1]},
            "locale": config.locale,
            "timezone_id": config.timezone,
            "accept_downloads": True,
            "downloads_path": str(self.download_dir) if self.download_dir else None,
            **self._credentials_kwargs(),
        }
        if config.device:
            device = self._pw.devices.get(config.device) if self._pw else None
            if device:
                merged = dict(device)
                merged.update(kwargs)
                kwargs = merged
        if config.geolocation:
            kwargs["geolocation"] = {
                "latitude": config.geolocation[0],
                "longitude": config.geolocation[1],
            }
            kwargs["permissions"] = ["geolocation"]
        if config.mode in ("ephemeral", "cdp"):
            try:
                state = storage_state_from_env(config)
            except Exception:
                logger.warning(
                    "browser storage state could not be loaded; continuing without it",
                    exc_info=True,
                )
                state = None
            if state:
                kwargs["storage_state"] = state
        return kwargs

    def _credentials_kwargs(self) -> dict[str, Any]:
        if self.config.http_user:
            return {
                "http_credentials": {
                    "username": self.config.http_user,
                    "password": self.config.http_pass or "",
                }
            }
        return {}

    def _prepare_work_dir(self) -> None:
        if self.work_dir is not None:
            return
        base = self.config.download_dir or Path(tempfile.gettempdir()) / "jarvis-browser"
        base.mkdir(parents=True, exist_ok=True)
        self.work_dir = Path(tempfile.mkdtemp(prefix="job-", dir=base))
        self.download_dir = self.work_dir / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        if self._closed and not self._started:
            return
        self._closed = True
        self._started = False
        self._cancel_dialog_task()

        page, context, browser, pw = (
            self._page,
            self._context,
            self._browser,
            self._pw,
        )
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        self._created_pages.clear()

        if self.config.mode == "cdp":
            for created in [page] if page else []:
                try:
                    await created.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    logger.debug("could not disconnect from the CDP browser", exc_info=True)
        else:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    logger.debug("could not close the browser context", exc_info=True)
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    logger.debug("could not close the browser", exc_info=True)

        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                logger.debug("could not stop Playwright", exc_info=True)

        self.pending_dialog = None

    # ------------------------------------------------------------------ events
    def _install_page(self, page: Page) -> None:
        page.set_default_timeout(self.config.action_timeout_ms)
        page.set_default_navigation_timeout(self.config.timeout_ms)
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfinished", self._on_request_finished)
        page.on("requestfailed", self._on_request_failed)
        page.on("framenavigated", self._on_frame_navigated)
        page.on("close", self._on_page_close)

    def _on_new_page(self, page: Page) -> None:
        self._install_page(page)
        owned = self.config.mode != "cdp" or self._in_action > 0
        if owned:
            self._created_pages.add(page)
        logger.info("new tab opened: %s", page.url)

    async def _on_dialog(self, dialog: Dialog) -> None:
        self.pending_dialog = dialog
        self.last_dialog = (dialog.type, dialog.message)
        logger.info("dialog opened: %s %r", dialog.type, dialog.message[:120])

        if dialog.type == "beforeunload":
            try:
                await dialog.accept()
            except Exception:
                pass
            self.pending_dialog = None
            return

        self._cancel_dialog_task()
        self._dialog_task = asyncio.create_task(self._auto_dismiss(dialog))

    async def _auto_dismiss(self, dialog: Dialog) -> None:
        try:
            await asyncio.sleep(DIALOG_GRACE_S)
            if self.pending_dialog is dialog:
                await dialog.dismiss()
                self.pending_dialog = None
                logger.info("auto-dismissed %s dialog after grace period", dialog.type)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("dialog auto-dismiss failed", exc_info=True)

    def _cancel_dialog_task(self) -> None:
        task = self._dialog_task
        self._dialog_task = None
        if task is not None and not task.done():
            task.cancel()

    async def resolve_dialog(self, action: str, prompt_text: str | None = None) -> str:
        dialog = self.pending_dialog
        if dialog is None:
            raise InvalidArguments("There is no dialog waiting on the page.")
        self._cancel_dialog_task()
        try:
            if action == "accept":
                await dialog.accept(prompt_text)
            elif action == "dismiss":
                await dialog.dismiss()
            else:
                raise InvalidArguments("dialog action must be accept or dismiss.")
        finally:
            self.pending_dialog = None
            self.bump_epoch()
        kind = dialog.type
        message = dialog.message
        return f'{action}ed the {kind} dialog "{message[:160]}".'

    def _on_download(self, download: Download) -> None:
        self._downloads.append(download)
        logger.info("download started: %s", download.suggested_filename)

    def _on_page_close(self, page: Page) -> None:
        self._created_pages.discard(page)
        if page is not self._page:
            return
        remaining = [p for p in (self._context.pages if self._context else []) if not p.is_closed()]
        self._page = remaining[0] if remaining else None
        self.bump_epoch()

    def _on_context_close(self, *_: Any) -> None:
        self._page = None

    def _on_frame_navigated(self, frame) -> None:
        if self._page is not None and frame is self._page.main_frame:
            self.bump_epoch()

    def _on_request(self, request) -> None:
        record = {
            "method": request.method,
            "url": request.url,
            "type": request.resource_type,
            "status": None,
            "duration_ms": None,
            "failed": None,
            "started": time.monotonic(),
        }
        self._requests.append(record)
        self._request_records[id(request)] = record

    def _record_for(self, request) -> dict[str, Any] | None:
        return self._request_records.get(id(request))

    def _on_response(self, response) -> None:
        record = self._record_for(response.request)
        if record is not None:
            record["status"] = response.status

    def _on_request_finished(self, request) -> None:
        record = self._record_for(request)
        if record is not None:
            record["duration_ms"] = int((time.monotonic() - record["started"]) * 1000)

    def _on_request_failed(self, request) -> None:
        record = self._record_for(request)
        if record is not None:
            record["failed"] = (request.failure or "failed")[:120]
            record["duration_ms"] = int((time.monotonic() - record["started"]) * 1000)

    # ---------------------------------------------------------------- networks
    def blocked_patterns(self) -> list[str]:
        return list(self._blocked)

    def block_pattern(self, pattern: str) -> None:
        self._blocked.append(pattern)

    def clear_blocked(self) -> None:
        self._blocked.clear()

    def mocks(self) -> list[tuple[str, int, str]]:
        return list(self._mocks)

    def add_mock(self, pattern: str, status: int, body: str) -> None:
        self._mocks.append((pattern, status, body))

    def clear_mocks(self) -> None:
        self._mocks.clear()

    def set_offline(self, value: bool) -> None:
        self._offline = value

    @property
    def offline(self) -> bool:
        return self._offline

    async def apply_network_rules(self) -> None:
        context = self._context
        if context is None:
            raise NotStarted("No browser is running yet.")
        await context.set_offline(self._offline)
        await context.set_extra_http_headers(self._extra_headers or {})

    async def _route(self, route) -> None:
        url = route.request.url
        for pattern in list(self._blocked):
            if pattern and pattern in url:
                await route.abort("blockedbyclient")
                return
        for pattern, status, body in list(self._mocks):
            if pattern and pattern in url:
                await route.fulfill(status=status, content_type="application/json", body=body)
                return
        await guard_request(route, self.config)

    async def handle_dialog(self, action: str, prompt_text: str | None = None) -> str:
        async with self._lock:
            return await self.resolve_dialog(action, prompt_text)

    # ------------------------------------------------------------------ pages
    async def new_tab(self) -> Page:
        context = self._context
        if context is None:
            raise NotStarted("No browser is running yet.")
        page = await context.new_page()
        self._created_pages.add(page)
        self._page = page
        self.bump_epoch()
        return page

    def set_active_page(self, index: int) -> Page:
        context = self._context
        if context is None:
            raise NotStarted("No browser is running yet.")
        pages = context.pages
        if index < 0 or index >= len(pages):
            raise InvalidArguments(f"Tab {index} does not exist; there are {len(pages)} tabs.")
        page = pages[index]
        self._page = page
        self.bump_epoch()
        return page

    def list_pages(self) -> list[Page]:
        context = self._context
        if context is None:
            return []
        return list(context.pages)

    async def close_tab(self, index: int) -> str:
        context = self._context
        if context is None:
            raise NotStarted("No browser is running yet.")
        pages = context.pages
        if index < 0 or index >= len(pages):
            raise InvalidArguments(f"Tab {index} does not exist; there are {len(pages)} tabs.")
        if len(pages) <= 1:
            raise InvalidArguments("I will not close the last tab. Use open_url to navigate instead.")
        page = pages[index]
        title = await page.title()
        await page.close()
        remaining = context.pages
        self._page = remaining[0] if remaining else None
        self.bump_epoch()
        return f'closed tab {index} ("{title[:60]}").'

    # ----------------------------------------------------------------- guards
    def ensure_crash_free(self) -> None:
        if self._closed and self._started:
            raise BrowserCrashed("The browser closed unexpectedly.")
