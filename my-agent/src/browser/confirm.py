"""Structural approval gate for consequential browser actions.

Prompt-only confirmation fails exactly when the model is eager. This gate is
code: it does not try to classify intent, it proves that a review was spoken
and that the user replied afterwards.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .config import APPROVAL_TTL_S
from .errors import ConfirmationRejected

if TYPE_CHECKING:
    from livekit.agents import RunContext

    from .session import BrowserSession

logger = logging.getLogger("browser.confirm")


@dataclass(slots=True)
class PendingAction:
    tool: str
    params: dict[str, Any]
    summary: str
    page_url: str
    frame_url: str
    fingerprint: str
    staged_after_user_item_id: str
    epoch: int
    staged_at: float = field(default_factory=time.monotonic)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


def last_user_item_id(ctx: RunContext) -> str:
    items = list(ctx.session.history.items)
    for item in reversed(items):
        if getattr(item, "type", None) == "message" and getattr(item, "role", None) == "user":
            return str(getattr(item, "id", "") or "")
    return ""


def _review_after_user(ctx: RunContext, user_item_id: str) -> int | None:
    """Index of the first non-interrupted assistant message with speech after ``user_item_id``."""
    items = list(ctx.session.history.items)
    start = None
    for index, item in enumerate(items):
        if str(getattr(item, "id", "") or "") == user_item_id:
            start = index
            break
    if start is None:
        return None

    for index in range(start + 1, len(items)):
        item = items[index]
        if getattr(item, "type", None) != "message":
            continue
        if getattr(item, "role", None) != "assistant":
            continue
        if getattr(item, "interrupted", False):
            continue
        text = getattr(item, "text_content", "") or ""
        if text.strip():
            return index
    return None


def _latest_user_index(ctx: RunContext) -> int | None:
    items = list(ctx.session.history.items)
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if getattr(item, "type", None) == "message" and getattr(item, "role", None) == "user":
            return index
    return None


class ApprovalStore:
    """One pending action per session. Staging a new one invalidates the old."""
    def __init__(self) -> None:
        self._pending: PendingAction | None = None

    @property
    def pending(self) -> PendingAction | None:
        return self._pending

    def stage(self, action: PendingAction) -> str:
        self._pending = action
        logger.info(
            "staged %s (%s) for confirmation: %s",
            action.tool,
            action.id,
            action.summary,
        )
        return (
            f"Confirmation required before executing: {action.summary}. "
            f"Ask the user to confirm, then call confirm_browser_action with "
            f'action_id="{action.id}".'
        )

    def clear(self) -> None:
        self._pending = None

    def validate(
        self, ctx: RunContext, session: BrowserSession, action_id: str
    ) -> PendingAction:
        pending = self._pending
        if pending is None:
            raise ConfirmationRejected(
                "There is nothing waiting for confirmation.",
                hint="Stage the action again if you still want to do it.",
            )

        if action_id and action_id != pending.id:
            raise ConfirmationRejected(
                f"The action id {action_id!r} does not match the pending action.",
                hint=f'The pending id is "{pending.id}".',
            )

        if time.monotonic() - pending.staged_at > APPROVAL_TTL_S:
            self._pending = None
            raise ConfirmationRejected(
                "That confirmation expired, Sir. The action was not performed.",
                hint="Ask the user again and stage it fresh.",
            )

        if session.epoch != pending.epoch:
            self._pending = None
            raise ConfirmationRejected(
                "The page changed after the action was staged, so it was not performed.",
                hint="Look at the page again and stage it once more.",
            )

        if session.pending_dialog is not None:
            raise ConfirmationRejected(
                "A dialog is waiting on the page.",
                hint="Call handle_dialog first, then confirm.",
            )

        if last_user_item_id(ctx) != pending.staged_after_user_item_id:
            # The user has spoken since staging, which is expected. This check
            # only guards against history being rewritten underneath us.
            logger.debug("approval staged against a different user message")

        review_index = _review_after_user(ctx, pending.staged_after_user_item_id)
        if review_index is None:
            raise ConfirmationRejected(
                "I have not described that action to the user yet, so I cannot do it.",
                hint="Describe what will happen and ask for a clear yes first.",
            )

        latest_user = _latest_user_index(ctx)
        if latest_user is None or latest_user <= review_index:
            raise ConfirmationRejected(
                "The user has not replied since I described the action.",
                hint="Ask the user to confirm, then call this tool again.",
            )

        return pending


def stage_pending(
    store: ApprovalStore,
    ctx: RunContext,
    session: BrowserSession,
    *,
    tool: str,
    params: dict[str, Any],
    summary: str,
    fingerprint: str,
    frame_index: int = 0,
) -> str:
    page = session.active_page()
    action = PendingAction(
        tool=tool,
        params=params,
        summary=summary,
        page_url=page.url,
        frame_url=session.frame_url(frame_index),
        fingerprint=fingerprint,
        staged_after_user_item_id=last_user_item_id(ctx),
        epoch=session.epoch,
    )
    return store.stage(action)
