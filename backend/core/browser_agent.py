"""
Browser automation via Playwright.

Drives a real Chromium instance for tasks beyond simply opening a URL:
- YouTube: search and play first result
- ChatGPT: open, type question, send

The browser uses a persistent profile (data/browser_profile/) so the
user's logins survive across CHAPPIE restarts. The browser is launched
lazily on first use; if Playwright isn't installed, BrowserAgent falls
back to opening the search URL in the user's default browser so the
user still gets the closest possible result.
"""

import asyncio
import os
import webbrowser
from pathlib import Path
from typing import Dict, Optional

from utils.logger import setup_logger

logger = setup_logger(__name__)


class BrowserAgent:
    """Playwright-based browser automation."""

    def __init__(self, kb, config: Dict):
        self.kb = kb
        self.config = config
        self._playwright = None
        self._context = None
        self._lock = asyncio.Lock()
        self._available: Optional[bool] = None

        bcfg = config.get("browser_automation", {})
        self.headless: bool = bcfg.get("headless", False)
        self.user_data_dir: str = os.path.expanduser(
            bcfg.get("user_data_dir", "data/browser_profile")
        )
        self.timeout_ms: int = int(bcfg.get("timeout_ms", 15000))

    # ------------------------------------------------------------------
    # Availability + lifecycle
    # ------------------------------------------------------------------

    async def is_available(self) -> bool:
        """Cheap check — does importing Playwright work?"""
        if self._available is not None:
            return self._available
        try:
            import playwright  # noqa: F401
            from playwright.async_api import async_playwright  # noqa: F401

            self._available = True
        except ImportError:
            logger.warning(
                "Playwright not installed; browser automation will fall back "
                "to plain URL opens. Install with: pip install playwright && "
                "playwright install chromium"
            )
            self._available = False
        return self._available

    async def _ensure_browser(self):
        """Launch the persistent browser context on first call."""
        async with self._lock:
            if self._context is not None:
                return

            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)

            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=self.headless,
            )
            logger.info(f"Browser launched (headless={self.headless})")

    async def close(self):
        async with self._lock:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception as exc:
                    logger.warning(f"Error closing browser context: {exc}")
                self._context = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    logger.warning(f"Error stopping playwright: {exc}")
                self._playwright = None

    # ------------------------------------------------------------------
    # Task: YouTube search and play
    # ------------------------------------------------------------------

    async def youtube_search_and_play(self, query: str) -> Dict:
        """Open YouTube, run a search for `query`, click the first video."""
        if not query:
            return {"success": False, "error": "empty query"}

        if not await self.is_available():
            return self._fallback_search("youtube", query)

        try:
            await self._ensure_browser()
            page = await self._context.new_page()

            search_url = (
                "https://www.youtube.com/results?search_query="
                + query.replace(" ", "+")
            )
            await page.goto(search_url, timeout=self.timeout_ms)

            # The selector for the first video link has shifted over the years —
            # try a few before giving up.
            selectors = [
                "a#video-title-link",
                "ytd-video-renderer a#video-title",
                "a#video-title",
                'a[href^="/watch"]',
            ]
            first = None
            for sel in selectors:
                try:
                    await page.wait_for_selector(sel, timeout=4000)
                    first = page.locator(sel).first
                    break
                except Exception:
                    continue

            if first is None:
                return {
                    "success": False,
                    "task": "youtube_play",
                    "query": query,
                    "url": page.url,
                    "message": (
                        f"I opened YouTube search for '{query}' but couldn't "
                        "find the result list. Pick one yourself — I'll learn next time."
                    ),
                }

            await first.click()
            try:
                await page.wait_for_selector("video", timeout=self.timeout_ms)
            except Exception:
                pass  # Even without the video element, we still navigated.

            return {
                "success": True,
                "task": "youtube_play",
                "query": query,
                "url": page.url,
                "message": f"Playing the top YouTube result for '{query}'.",
            }
        except Exception as exc:
            logger.error(f"YouTube automation failed: {exc}")
            fallback = self._fallback_search("youtube", query)
            fallback["error"] = str(exc)
            return fallback

    # ------------------------------------------------------------------
    # Task: ChatGPT ask
    # ------------------------------------------------------------------

    async def chatgpt_ask(self, question: str) -> Dict:
        """Open ChatGPT, type the question into the prompt, hit Enter."""
        if not question:
            return {"success": False, "error": "empty question"}

        if not await self.is_available():
            return self._fallback_chatgpt(question)

        try:
            await self._ensure_browser()
            page = await self._context.new_page()

            await page.goto("https://chatgpt.com", timeout=self.timeout_ms)

            # ChatGPT swaps selectors regularly — try a list.
            selectors = [
                "#prompt-textarea",
                "textarea[data-id]",
                'textarea[placeholder*="Message"]',
                'textarea[placeholder*="message"]',
                'div[contenteditable="true"]',
            ]
            target = None
            for sel in selectors:
                try:
                    await page.wait_for_selector(sel, timeout=4000)
                    target = page.locator(sel).first
                    break
                except Exception:
                    continue

            if target is None:
                return {
                    "success": False,
                    "task": "chatgpt_ask",
                    "question": question,
                    "url": page.url,
                    "message": (
                        "I opened ChatGPT but couldn't find the input — you may "
                        "need to log in. After you log in once it'll remember you."
                    ),
                }

            try:
                await target.fill(question)
            except Exception:
                # Contenteditable divs can't be .fill()'d — type instead.
                await target.click()
                await page.keyboard.type(question)

            await page.keyboard.press("Enter")

            return {
                "success": True,
                "task": "chatgpt_ask",
                "question": question,
                "url": page.url,
                "message": (
                    "Asked ChatGPT: "
                    f"'{question[:80]}{'...' if len(question) > 80 else ''}'"
                ),
            }
        except Exception as exc:
            logger.error(f"ChatGPT automation failed: {exc}")
            fallback = self._fallback_chatgpt(question)
            fallback["error"] = str(exc)
            return fallback

    # ------------------------------------------------------------------
    # Fallbacks (when Playwright is missing or the page changed)
    # ------------------------------------------------------------------

    def _fallback_search(self, engine: str, query: str) -> Dict:
        if engine == "youtube":
            url = (
                "https://www.youtube.com/results?search_query="
                + query.replace(" ", "+")
            )
        else:
            url = "https://www.google.com/search?q=" + query.replace(" ", "+")
        webbrowser.open(url)
        return {
            "success": True,
            "task": f"{engine}_search_fallback",
            "query": query,
            "url": url,
            "message": (
                f"Opening {engine} search for '{query}'. "
                "(Install Playwright for hands-free playback.)"
            ),
        }

    def _fallback_chatgpt(self, question: str) -> Dict:
        clipboard_msg = ""
        try:
            import pyperclip  # type: ignore

            pyperclip.copy(question)
            clipboard_msg = " (your question is on the clipboard — paste it in)"
        except ImportError:
            pass
        webbrowser.open("https://chatgpt.com")
        return {
            "success": True,
            "task": "chatgpt_fallback",
            "question": question,
            "url": "https://chatgpt.com",
            "message": f"Opening ChatGPT{clipboard_msg}.",
        }
