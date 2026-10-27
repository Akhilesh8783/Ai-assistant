"""Playwright-backed browser control for the Jarvis voice agent."""

from __future__ import annotations

from .manager import BrowserError, BrowserManager

__all__ = ["BrowserManager", "BrowserError"]

__version__ = "1.0.0"
