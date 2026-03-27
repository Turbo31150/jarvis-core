#!/usr/bin/env python3
"""
JARVIS Browser Agent — Chrome DevTools Protocol wrapper for browser automation.

Connects to BrowserOS via CDP on port 9105.
Provides both async (BrowserAgent) and sync (BrowserAgentSync) interfaces.

Usage:
    from browser_agent import BrowserAgent, BrowserAgentSync

    # Async
    async with BrowserAgent() as agent:
        await agent.navigate("https://example.com")
        title = await agent.get_page_title()

    # Sync
    with BrowserAgentSync() as agent:
        agent.navigate("https://example.com")
        title = agent.get_page_title()
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import websockets

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CDP_URL = os.getenv("CHROME_CDP_URL", "http://127.0.0.1:9105")
CDP_HOST = CDP_URL.replace("http://", "").replace("https://", "")
LOG_PATH = Path(os.getenv(
    "BROWSER_AGENT_LOG",
    "/home/turbo/IA/Core/jarvis/logs/browser-agent.log",
))
CONNECT_TIMEOUT = int(os.getenv("BROWSER_AGENT_CONNECT_TIMEOUT", "10"))
DEFAULT_TIMEOUT = int(os.getenv("BROWSER_AGENT_DEFAULT_TIMEOUT", "30"))
RECONNECT_DELAY = int(os.getenv("BROWSER_AGENT_RECONNECT_DELAY", "2"))
MAX_RECONNECT = int(os.getenv("BROWSER_AGENT_MAX_RECONNECT_ATTEMPTS", "5"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("jarvis.browser_agent")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_ws_url(page_index: int = 0) -> str:
    """Fetch the WebSocket debugger URL from the CDP /json endpoint."""
    import urllib.request
    url = f"http://{CDP_HOST}/json"
    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(None, urllib.request.urlopen, url)
    pages = json.loads(resp.read())
    if not pages:
        raise ConnectionError("No targets found on CDP endpoint")
    if page_index >= len(pages):
        page_index = 0
    return pages[page_index]["webSocketDebuggerUrl"]


async def _get_browser_ws_url() -> str:
    """Fetch the browser-level WebSocket URL."""
    import urllib.request
    url = f"http://{CDP_HOST}/json/version"
    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(None, urllib.request.urlopen, url)
    info = json.loads(resp.read())
    return info["webSocketDebuggerUrl"]


# ---------------------------------------------------------------------------
# BrowserAgent (async)
# ---------------------------------------------------------------------------


class BrowserAgent:
    """Async CDP browser automation agent for JARVIS."""

    def __init__(self, cdp_host: str | None = None):
        self._cdp_host = cdp_host or CDP_HOST
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._browser_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._msg_id = 0
        self._connected = False

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the first available CDP page target."""
        for attempt in range(1, MAX_RECONNECT + 1):
            try:
                ws_url = await _get_ws_url()
                self._ws = await asyncio.wait_for(
                    websockets.connect(ws_url, max_size=50 * 1024 * 1024),
                    timeout=CONNECT_TIMEOUT,
                )
                self._connected = True
                logger.info("Connected to page target: %s", ws_url)
                return
            except Exception as exc:
                logger.warning("Connect attempt %d/%d failed: %s", attempt, MAX_RECONNECT, exc)
                if attempt < MAX_RECONNECT:
                    await asyncio.sleep(RECONNECT_DELAY)
        raise ConnectionError(f"Failed to connect after {MAX_RECONNECT} attempts")

    async def connect_browser(self) -> None:
        """Connect to the browser-level target (for tab management)."""
        ws_url = await _get_browser_ws_url()
        self._browser_ws = await asyncio.wait_for(
            websockets.connect(ws_url, max_size=50 * 1024 * 1024),
            timeout=CONNECT_TIMEOUT,
        )
        logger.info("Connected to browser target: %s", ws_url)

    async def disconnect(self) -> None:
        for ws in (self._ws, self._browser_ws):
            if ws:
                await ws.close()
        self._ws = None
        self._browser_ws = None
        self._connected = False
        logger.info("Disconnected")

    async def _reconnect(self) -> None:
        logger.info("Attempting reconnect...")
        await self.disconnect()
        await self.connect()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    # -- low-level CDP ------------------------------------------------------

    async def _send(self, method: str, params: dict | None = None,
                    ws: Optional[websockets.WebSocketClientProtocol] = None,
                    timeout: float = DEFAULT_TIMEOUT) -> dict:
        ws = ws or self._ws
        if not ws:
            raise ConnectionError("Not connected")
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        try:
            await ws.send(json.dumps(msg))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                resp = json.loads(raw)
                if resp.get("id") == self._msg_id:
                    if "error" in resp:
                        raise RuntimeError(f"CDP error: {resp['error']}")
                    return resp.get("result", {})
        except (websockets.ConnectionClosed, asyncio.TimeoutError) as exc:
            logger.error("CDP send failed (%s): %s", method, exc)
            await self._reconnect()
            raise

    # -- navigation ---------------------------------------------------------

    async def navigate(self, url: str) -> dict:
        """Navigate current page to URL."""
        logger.info("navigate -> %s", url)
        result = await self._send("Page.navigate", {"url": url})
        await asyncio.sleep(0.5)
        return result

    async def get_url(self) -> str:
        r = await self._send("Runtime.evaluate", {"expression": "window.location.href"})
        return r["result"]["value"]

    async def get_page_title(self) -> str:
        r = await self._send("Runtime.evaluate", {"expression": "document.title"})
        return r["result"]["value"]

    # -- interaction --------------------------------------------------------

    async def evaluate(self, js_code: str) -> Any:
        """Execute arbitrary JavaScript and return the result."""
        r = await self._send("Runtime.evaluate", {
            "expression": js_code,
            "returnByValue": True,
            "awaitPromise": True,
        })
        return r.get("result", {}).get("value")

    async def click(self, selector: str) -> None:
        logger.info("click -> %s", selector)
        js = f"document.querySelector({json.dumps(selector)})?.click()"
        await self.evaluate(js)

    async def fill(self, selector: str, text: str) -> None:
        logger.info("fill -> %s", selector)
        js = f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error('Element not found: {selector}');
            el.focus();
            el.value = {json.dumps(text)};
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
        }})()
        """
        await self.evaluate(js)

    async def type_text(self, selector: str, text: str) -> None:
        """Type text character by character (triggers key events)."""
        logger.info("type_text -> %s", selector)
        focus_js = f"document.querySelector({json.dumps(selector)})?.focus()"
        await self.evaluate(focus_js)
        for char in text:
            await self._send("Input.dispatchKeyEvent", {
                "type": "keyDown", "text": char, "key": char,
            })
            await self._send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": char,
            })
            await asyncio.sleep(0.02)

    async def get_text(self, selector: str) -> str:
        js = f"document.querySelector({json.dumps(selector)})?.textContent || ''"
        return await self.evaluate(js)

    async def get_elements(self, selector: str) -> list[dict]:
        """Get all matching elements with text content and key attributes."""
        js = f"""
        Array.from(document.querySelectorAll({json.dumps(selector)})).map(el => ({{
            tag: el.tagName.toLowerCase(),
            text: el.textContent?.trim().slice(0, 200) || '',
            id: el.id || null,
            className: el.className || null,
            href: el.href || null,
            value: el.value || null,
        }}))
        """
        return await self.evaluate(js) or []

    async def wait_for(self, selector: str, timeout: float = 10) -> bool:
        """Wait for an element to appear in the DOM."""
        logger.info("wait_for -> %s (timeout=%s)", selector, timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = await self.evaluate(
                f"!!document.querySelector({json.dumps(selector)})"
            )
            if found:
                return True
            await asyncio.sleep(0.3)
        logger.warning("wait_for timed out: %s", selector)
        return False

    async def scroll(self, direction: str = "down", amount: int = 500) -> None:
        dy = amount if direction == "down" else -amount
        await self.evaluate(f"window.scrollBy(0, {dy})")

    async def copy_text(self, selector: str) -> str:
        return await self.get_text(selector)

    async def paste_text(self, selector: str, text: str) -> None:
        await self.fill(selector, text)

    async def submit_form(self, form_selector: str) -> None:
        js = f"document.querySelector({json.dumps(form_selector)})?.submit()"
        await self.evaluate(js)

    # -- screenshots --------------------------------------------------------

    async def screenshot(self, filepath: str) -> str:
        """Take a screenshot and save to filepath. Returns the path."""
        import base64
        logger.info("screenshot -> %s", filepath)
        r = await self._send("Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(r["data"])
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        Path(filepath).write_bytes(data)
        return filepath

    # -- tab management -----------------------------------------------------

    async def list_tabs(self) -> list[dict]:
        """List all open browser targets/tabs."""
        import urllib.request
        url = f"http://{self._cdp_host}/json"
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, urllib.request.urlopen, url)
        pages = json.loads(resp.read())
        return [
            {"id": p["id"], "title": p.get("title", ""), "url": p.get("url", ""),
             "type": p.get("type", "")}
            for p in pages if p.get("type") == "page"
        ]

    async def new_tab(self, url: str = "about:blank") -> dict:
        """Open a new tab."""
        logger.info("new_tab -> %s", url)
        if not self._browser_ws:
            await self.connect_browser()
        result = await self._send("Target.createTarget", {"url": url},
                                  ws=self._browser_ws)
        return result

    async def switch_tab(self, tab_id: str) -> None:
        """Switch to a specific tab by its target ID."""
        logger.info("switch_tab -> %s", tab_id)
        if not self._browser_ws:
            await self.connect_browser()
        await self._send("Target.activateTarget", {"targetId": tab_id},
                         ws=self._browser_ws)
        # Reconnect page-level WS to the new target
        ws_url = f"ws://{self._cdp_host}/devtools/page/{tab_id}"
        if self._ws:
            await self._ws.close()
        self._ws = await websockets.connect(ws_url, max_size=50 * 1024 * 1024)

    async def close_tab(self, tab_id: str) -> None:
        logger.info("close_tab -> %s", tab_id)
        if not self._browser_ws:
            await self.connect_browser()
        await self._send("Target.closeTarget", {"targetId": tab_id},
                         ws=self._browser_ws)

    async def find_tab_by_url(self, url_pattern: str) -> Optional[dict]:
        """Find a tab whose URL matches the pattern (regex)."""
        tabs = await self.list_tabs()
        for tab in tabs:
            if re.search(url_pattern, tab["url"]):
                return tab
        return None

    async def find_tab_by_title(self, title_pattern: str) -> Optional[dict]:
        """Find a tab whose title matches the pattern (regex)."""
        tabs = await self.list_tabs()
        for tab in tabs:
            if re.search(title_pattern, tab["title"], re.IGNORECASE):
                return tab
        return None

    async def tab_group_create(self, name: str, tab_ids: list[str]) -> None:
        """Group tabs (Chrome-only, uses JS bridge)."""
        logger.info("tab_group_create -> %s (%d tabs)", name, len(tab_ids))
        # Tab grouping via CDP requires chrome.tabGroups API which is
        # extension-only. Log intent; actual grouping requires extension support.
        logger.warning("Tab grouping is limited via CDP; logged group '%s' with %d tabs", name, len(tab_ids))


# ---------------------------------------------------------------------------
# BrowserAgentSync — synchronous wrapper
# ---------------------------------------------------------------------------


class BrowserAgentSync:
    """Synchronous wrapper around BrowserAgent for non-async code."""

    def __init__(self, cdp_host: str | None = None):
        self._agent = BrowserAgent(cdp_host)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run(self, coro):
        return self._get_loop().run_until_complete(coro)

    def connect(self): self._run(self._agent.connect())
    def disconnect(self): self._run(self._agent.disconnect())

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()
        if self._loop and not self._loop.is_closed():
            self._loop.close()

    # Expose all public methods as sync
    def navigate(self, url: str): return self._run(self._agent.navigate(url))
    def click(self, selector: str): return self._run(self._agent.click(selector))
    def fill(self, selector: str, text: str): return self._run(self._agent.fill(selector, text))
    def type_text(self, selector: str, text: str): return self._run(self._agent.type_text(selector, text))
    def screenshot(self, filepath: str): return self._run(self._agent.screenshot(filepath))
    def get_text(self, selector: str): return self._run(self._agent.get_text(selector))
    def get_page_title(self) -> str: return self._run(self._agent.get_page_title())
    def get_url(self) -> str: return self._run(self._agent.get_url())
    def evaluate(self, js_code: str): return self._run(self._agent.evaluate(js_code))
    def list_tabs(self): return self._run(self._agent.list_tabs())
    def new_tab(self, url: str = "about:blank"): return self._run(self._agent.new_tab(url))
    def switch_tab(self, tab_id: str): return self._run(self._agent.switch_tab(tab_id))
    def close_tab(self, tab_id: str): return self._run(self._agent.close_tab(tab_id))
    def wait_for(self, selector: str, timeout: float = 10): return self._run(self._agent.wait_for(selector, timeout))
    def scroll(self, direction: str = "down", amount: int = 500): return self._run(self._agent.scroll(direction, amount))
    def copy_text(self, selector: str): return self._run(self._agent.copy_text(selector))
    def paste_text(self, selector: str, text: str): return self._run(self._agent.paste_text(selector, text))
    def get_elements(self, selector: str): return self._run(self._agent.get_elements(selector))
    def submit_form(self, form_selector: str): return self._run(self._agent.submit_form(form_selector))
    def find_tab_by_url(self, url_pattern: str): return self._run(self._agent.find_tab_by_url(url_pattern))
    def find_tab_by_title(self, title_pattern: str): return self._run(self._agent.find_tab_by_title(title_pattern))
    def tab_group_create(self, name: str, tab_ids: list[str]): return self._run(self._agent.tab_group_create(name, tab_ids))


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    async def demo():
        print(f"Connecting to BrowserOS CDP at {CDP_URL}...")
        async with BrowserAgent() as agent:
            # List open tabs
            tabs = await agent.list_tabs()
            print(f"\nOpen tabs ({len(tabs)}):")
            for t in tabs:
                print(f"  [{t['id'][:8]}] {t['title']} - {t['url']}")

            # Navigate
            url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
            print(f"\nNavigating to {url}...")
            await agent.navigate(url)
            await asyncio.sleep(1)

            title = await agent.get_page_title()
            current_url = await agent.get_url()
            print(f"Title: {title}")
            print(f"URL:   {current_url}")

            # Screenshot
            screenshot_path = "/tmp/jarvis-browser-demo.png"
            await agent.screenshot(screenshot_path)
            print(f"Screenshot saved: {screenshot_path}")

            # Get all links
            links = await agent.get_elements("a")
            if links:
                print(f"\nLinks found ({len(links)}):")
                for link in links[:5]:
                    print(f"  {link.get('text', '?')} -> {link.get('href', '?')}")

        print("\nDone.")

    asyncio.run(demo())
