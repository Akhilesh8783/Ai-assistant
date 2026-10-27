from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

if TYPE_CHECKING:
    from browser import BrowserManager

logger = logging.getLogger("agent.tools")


class BrowserTools:
    """LiveKit agent tools for controlling a real browser via BrowserManager."""

    def __init__(self, browser: BrowserManager) -> None:
        self.browser = browser

    @property
    def tools(self) -> list:
        return [
            self.open_url,
            self.search_the_web,
            self.search_web,
            self.inspect_page,
            self.read_page,
            self.click,
            self.type_text,
            self.scroll,
            self.press_key,
            self.go_back,
            self.take_screenshot,
            self.confirm_browser_action,
        ]

    @function_tool()
    async def open_url(self, context: RunContext, url: str) -> str:
        """Open a website or URL directly in the browser.

        Args:
            url: The web address or domain to open (e.g. 'youtube.com', 'https://google.com', 'wikipedia.org').
        """
        try:
            logger.info("Opening URL: %s", url)
            res = await self.browser.open_url(url)
            return f"Successfully opened {res['url']}. Title: '{res['title']}'."
        except Exception as e:
            logger.error("Failed to open URL %s: %s", url, e)
            return f"Failed to open {url}: {e}"

    @function_tool()
    async def search_the_web(self, context: RunContext, query: str) -> str:
        """Search the web using DuckDuckGo in the browser.

        Use this tool when no specific website is named and a general web search is needed.

        Args:
            query: The search terms to look up.
        """
        try:
            logger.info("Searching web for: %s", query)
            res = await self.browser.search_duckduckgo(query)
            return (
                f"Searched for '{query}'. Current page: '{res['title']}' ({res['url']}). "
                "Call inspect_page or read_page to review the search results."
            )
        except Exception as e:
            logger.error("Search failed for %s: %s", query, e)
            return f"Search failed: {e}"

    @function_tool()
    async def search_web(self, context: RunContext, query: str) -> str:
        """Search the web for information related to the given query.

        Args:
            query: The search query.
        """
        return await self.search_the_web(context, query)

    @function_tool()
    async def inspect_page(self, context: RunContext) -> str:
        """Inspect the current web page to view its text summary and a list of clickable elements and text inputs.

        Always call this before clicking or typing so you have the exact element names.
        """
        try:
            res = await self.browser.inspect_page()
            elements = res.get("elements", [])
            lines = [f"- [{el['role']}] \"{el['name']}\"" for el in elements]
            elements_text = "\n".join(lines) if lines else "No interactive elements detected."
            return (
                f"Page Title: {res['title']}\n"
                f"URL: {res['url']}\n"
                f"Content preview: {res['text']}\n\n"
                f"Available interactive controls:\n{elements_text}"
            )
        except Exception as e:
            logger.error("Inspect page failed: %s", e)
            return f"Failed to inspect page: {e}"

    @function_tool()
    async def read_page(self, context: RunContext) -> str:
        """Read the main text content of the currently open webpage.

        Useful for reading articles, search results, or answers from a webpage.
        """
        try:
            res = await self.browser.read_page()
            trunc_note = " (content truncated for brevity)" if res.get("truncated") else ""
            return (
                f"Page Title: {res['title']}\n"
                f"URL: {res['url']}\n\n"
                f"Content{trunc_note}:\n{res['text']}"
            )
        except Exception as e:
            logger.error("Read page failed: %s", e)
            return f"Failed to read page: {e}"

    @function_tool()
    async def click(self, context: RunContext, target: str) -> str:
        """Click on a button, link, or clickable element on the current page.

        Args:
            target: The name, text, label, or role of the element to click (from inspect_page).
        """
        try:
            logger.info("Clicking target: %s", target)
            res = await self.browser.click(target)
            return f"Clicked '{target}'. Current page: '{res['title']}' ({res['url']})."
        except Exception as e:
            logger.error("Click failed on '%s': %s", target, e)
            return f"Could not click '{target}': {e}. Try calling inspect_page to see the latest controls."

    @function_tool()
    async def type_text(self, context: RunContext, target: str, text: str) -> str:
        """Type text into an input box, search field, or textarea on the active page.

        Args:
            target: The name, label, or placeholder of the input field (e.g. 'Search', 'q').
            text: The text string to type into the field.
        """
        try:
            logger.info("Typing '%s' into '%s'", text, target)
            res = await self.browser.type_text(target, text)
            return f"Typed '{text}' into '{target}'."
        except Exception as e:
            logger.error("Type text failed on '%s': %s", target, e)
            return f"Could not type into '{target}': {e}. Try inspect_page to verify the input name."

    @function_tool()
    async def scroll(self, context: RunContext, direction: Literal["up", "down"] = "down") -> str:
        """Scroll the current webpage up or down.

        Args:
            direction: 'up' or 'down'. Defaults to 'down'.
        """
        try:
            res = await self.browser.scroll(direction)
            return f"Scrolled {direction}."
        except Exception as e:
            logger.error("Scroll failed: %s", e)
            return f"Failed to scroll: {e}"

    @function_tool()
    async def press_key(self, context: RunContext, key: str) -> str:
        """Press a keyboard key on the active page.

        Args:
            key: The key name to press (e.g. 'Enter', 'Escape', 'Tab', 'ArrowDown', 'ArrowUp', 'Backspace', 'Space').
        """
        try:
            res = await self.browser.press_key(key)
            return f"Pressed '{key}' on page {res['url']}."
        except Exception as e:
            logger.error("Press key failed: %s", e)
            return f"Could not press key '{key}': {e}"

    @function_tool()
    async def go_back(self, context: RunContext) -> str:
        """Navigate back to the previous webpage in browser history."""
        try:
            res = await self.browser.go_back()
            return f"Returned to: '{res['title']}' ({res['url']})."
        except Exception as e:
            logger.error("Go back failed: %s", e)
            return f"Failed to go back: {e}"

    @function_tool()
    async def take_screenshot(self, context: RunContext) -> str:
        """Capture a screenshot of the current webpage."""
        try:
            res = await self.browser.take_screenshot()
            return f"Captured screenshot ({res['bytes']} bytes) of {res['url']}."
        except Exception as e:
            logger.error("Screenshot failed: %s", e)
            return f"Failed to capture screenshot: {e}"

    @function_tool()
    async def confirm_browser_action(self, context: RunContext, action: str) -> str:
        """Confirm and execute a consequential browser action after user approval.

        Args:
            action: Brief description of the action being confirmed.
        """
        logger.info("Confirmed browser action: %s", action)
        return f"Action '{action}' has been confirmed by the user. You may proceed."


@function_tool()
async def search_web(context: RunContext, query: str) -> str:
    """Standalone fallback search tool using DuckDuckGo."""
    try:
        from importlib import import_module
        DuckDuckGoSearchRun = import_module("langchain_community.tools").DuckDuckGoSearchRun
        result = DuckDuckGoSearchRun().run(tool_input=query)
        logger.info("Search result for '%s': %s", query, result)
        return str(result)
    except Exception as e:
        logger.error("Fallback search failed for '%s': %e", query, e)
        return f"Search error: {e}"
