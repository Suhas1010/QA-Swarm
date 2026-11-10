from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Browser, BrowserContext, ConsoleMessage, Dialog, Page, Request, Response

XSS_INIT_SCRIPT = """
window.__qa_xss = false;
window.alert = function(msg) { window.__qa_xss = true; window.__qa_xss_msg = String(msg || 'alert'); };
window.confirm = function(msg) { window.__qa_xss = true; return true; };
window.prompt = function(msg) { window.__qa_xss = true; return ''; };
"""


@dataclass
class StepEvents:
    console_errors: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    network_failures: list[dict[str, Any]] = field(default_factory=list)
    xss_triggered: bool = False
    crashed: bool = False


class BrowserSession:
    """Isolated Playwright BrowserContext + Page with telemetry listeners and deterministic locator execution."""

    def __init__(
        self,
        browser: Browser,
        *,
        viewport_width: int = 1280,
        viewport_height: int = 800,
        has_touch: bool = False,
        record_video_dir: str | None = None,
        storage_state: dict[str, Any] | None = None,
    ) -> None:
        self.browser = browser
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.has_touch = has_touch
        self.record_video_dir = record_video_dir
        self.storage_state = storage_state
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._events = StepEvents()

    async def start(self) -> Page:
        ctx_kwargs: dict[str, Any] = {
            "viewport": {"width": self.viewport_width, "height": self.viewport_height},
            "has_touch": self.has_touch,
        }
        if self.storage_state is not None:
            ctx_kwargs["storage_state"] = self.storage_state
        if self.record_video_dir:
            ctx_kwargs["record_video_dir"] = self.record_video_dir
            ctx_kwargs["record_video_size"] = {
                "width": self.viewport_width,
                "height": self.viewport_height,
            }

        self.context = await self.browser.new_context(**ctx_kwargs)
        await self.context.add_init_script(XSS_INIT_SCRIPT)
        self.page = await self.context.new_page()
        self._attach_listeners(self.page)
        return self.page

    def _attach_listeners(self, page: Page) -> None:
        def on_console(msg: ConsoleMessage) -> None:
            if msg.type == "error":
                text = msg.text
                # Ignore benign favicon 404 console noise if unrelated
                if "favicon.ico" in text:
                    return
                self._events.console_errors.append(
                    {"type": msg.type, "text": text, "url": page.url}
                )

        def on_pageerror(err: Exception) -> None:
            self._events.page_errors.append(str(err))

        def on_response(res: Response) -> None:
            if res.status >= 400 and "favicon.ico" not in res.url:
                self._events.network_failures.append(
                    {
                        "status": res.status,
                        "method": res.request.method,
                        "url": res.url,
                    }
                )

        def on_requestfailed(req: Request) -> None:
            failure = req.failure or "requestfailed"
            if "ERR_ABORTED" in failure or "favicon.ico" in req.url:
                return
            self._events.network_failures.append(
                {
                    "status": 0,
                    "method": req.method,
                    "url": req.url,
                    "failure": failure,
                }
            )

        def on_dialog(dialog: Dialog) -> None:
            self._events.xss_triggered = True
            asyncio.create_task(dialog.dismiss())

        def on_crash(_: Page) -> None:
            self._events.crashed = True

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("response", on_response)
        page.on("requestfailed", on_requestfailed)
        page.on("dialog", on_dialog)
        page.on("crash", on_crash)

    def drain_events(self) -> StepEvents:
        current = self._events
        self._events = StepEvents()
        return current

    async def check_xss_sentinel(self) -> bool:
        if self.page is None:
            return False
        try:
            flagged = await self.page.evaluate("() => Boolean(window.__qa_xss)")
            if flagged:
                await self.page.evaluate("() => { window.__qa_xss = false; }")
            return bool(flagged)
        except Exception:
            return False

    async def settle(self, timeout_ms: int = 1200) -> None:
        if self.page is None:
            return
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 600))
        except Exception:
            pass
        await asyncio.sleep(0.1)

    async def _resolve_locator(self, locator_json: dict[str, Any]):
        assert self.page is not None
        candidates = []
        if locator_json.get("primary_selector"):
            candidates.append(self.page.locator(locator_json["primary_selector"]).first)
        if locator_json.get("role") and locator_json.get("role_name"):
            candidates.append(
                self.page.get_by_role(
                    locator_json["role"], name=locator_json["role_name"], exact=False
                ).first
            )
        if locator_json.get("css_path"):
            candidates.append(self.page.locator(locator_json["css_path"]).first)

        for loc in candidates:
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception:
                continue
        return candidates[0] if candidates else None

    async def execute_action(
        self,
        action_type: str,
        locator_json: dict[str, Any],
        value: str | None = None,
    ) -> None:
        assert self.page is not None
        act = action_type.lower().strip()

        if act == "navigate":
            target = (value or "").strip()
            if target:
                await self.page.goto(target, wait_until="domcontentloaded", timeout=10000)
            await self.settle()
            return

        if act == "back":
            await self.page.go_back(wait_until="domcontentloaded", timeout=6000)
            await self.settle()
            return

        if act == "wait":
            await asyncio.sleep(0.8)
            return

        if act == "press_key":
            key_name = (value or "Tab").strip()
            await self.page.keyboard.press(key_name)
            await self.settle()
            return

        if act == "scroll":
            dy = -500 if (value or "").lower() == "up" else 500
            await self.page.mouse.wheel(0, dy)
            await asyncio.sleep(0.35)
            return

        loc = await self._resolve_locator(locator_json)
        if loc is None:
            bbox = locator_json.get("bbox")
            if act in {"click", "dblclick"} and bbox:
                cx = bbox["x"] + bbox["w"] / 2
                cy = bbox["y"] + bbox["h"] / 2
                if act == "dblclick":
                    await self.page.mouse.dblclick(cx, cy)
                else:
                    await self.page.mouse.click(cx, cy)
                await self.settle()
                return
            raise RuntimeError(f"Could not resolve locator for action={act}: {locator_json}")

        if act == "click":
            await loc.click(timeout=4000)
        elif act == "dblclick":
            await loc.dblclick(timeout=4000)
        elif act == "type":
            await loc.fill(value or "", timeout=4000)
            # Trigger change event for inputs bound via onchange (e.g., cart qty)
            try:
                await loc.dispatch_event("change")
            except Exception:
                pass
        elif act == "select":
            await loc.select_option(value or "", timeout=4000)
        else:
            raise ValueError(f"Unsupported action_type: {action_type}")

        await self.settle()

    async def close(self) -> str | None:
        video_path: str | None = None
        if self.page is not None and self.record_video_dir:
            try:
                video = self.page.video
                await self.context.close()  # type: ignore[union-attr]
                if video is not None:
                    video_path = str(await video.path())
                self.context = None
                self.page = None
                return video_path
            except Exception:
                pass
        if self.context is not None:
            await self.context.close()
            self.context = None
            self.page = None
        return video_path

