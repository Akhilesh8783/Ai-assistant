"""Browser error taxonomy.

Every failure that the voice model can act on becomes a ``ToolError`` with a
short, speakable message. The model should never see a raw Playwright
traceback: it cannot do anything useful with one, and it wastes context.
"""

from __future__ import annotations

import logging

from livekit.agents import ToolError

logger = logging.getLogger("browser")


class BrowserError(Exception):
    """Base class for browser failures with a stable machine-readable code."""

    code = "browser_error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

    def tool_error(self) -> ToolError:
        text = str(self)
        if self.hint:
            text = f"{text} {self.hint}"
        return ToolError(text)


class InvalidArguments(BrowserError):
    code = "invalid_args"


class PolicyViolation(BrowserError):
    code = "url_blocked"


class NotStarted(BrowserError):
    code = "no_page_open"


class NavigationError(BrowserError):
    code = "navigation_failed"


class TimeoutFailure(BrowserError):
    code = "timeout"


class TargetNotFound(BrowserError):
    code = "target_not_found"


class StaleRef(TargetNotFound):
    code = "stale_ref"


class AmbiguousTarget(BrowserError):
    code = "ambiguous_target"


class BlockedBySite(BrowserError):
    code = "blocked_by_site"


class LoginRequired(BrowserError):
    code = "login_required"


class BrowserCrashed(BrowserError):
    code = "browser_crashed"


class ConfirmationRequired(BrowserError):
    code = "confirmation_required"


class ConfirmationRejected(BrowserError):
    code = "confirmation_rejected"


class DownloadFailed(BrowserError):
    code = "download_failed"


def to_tool_error(exc: BaseException) -> ToolError:
    """Convert any exception raised by the browser layer into a ``ToolError``."""
    if isinstance(exc, BrowserError):
        return exc.tool_error()

    # Imported lazily so that a Playwright import failure in a non-browser
    # environment does not break module import.
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except Exception:  # pragma: no cover - playwright is a hard dependency
        PlaywrightError = TimeoutError  # type: ignore[assignment,misc]

    if isinstance(exc, PlaywrightTimeoutError):
        return ToolError(
            "The browser operation timed out. The page may still be loading, "
            "or the target may not be available. Call inspect_page and try once more."
        )

    if isinstance(exc, PlaywrightError):
        message = str(exc).splitlines()[0][:300]
        if "Target page, context or browser has been closed" in message:
            return ToolError(
                "The browser was closed. Open a page again with open_url."
            )
        if "Execution context was destroyed" in message:
            return ToolError(
                "The page navigated while the action was running. Call inspect_page and retry."
            )
        return ToolError(f"The browser rejected that action: {message}")

    logger.exception("unexpected browser failure", exc_info=exc)
    return ToolError(
        "Something went wrong inside the browser. Try again, or open the page once more."
    )
