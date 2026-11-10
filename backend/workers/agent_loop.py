from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from google import genai
from google.genai import types
from playwright.async_api import Browser
from pydantic import BaseModel, Field

from config import Settings, estimate_cost_usd
from core.event_bus import EventBus
from core.llm_retry import (
    DEFAULT_NAV_FALLBACKS,
    call_gemini_with_fallback,
)
from core.queue import SuspectQueue
from detector.accessibility import audit_wcag_accessibility
from detector.objective import DetectedSuspect, evaluate_objective_signals, probe_page_health
from detector.visual_judge import VisualJudge
from storage.artifacts import ArtifactStore
from storage.db import Database
from workers.browser_session import BrowserSession, StepEvents
from workers.personas import PersonaSpec
from workers.som_grounding import capture_som_perception


class ActionDecision(BaseModel):
    thought: str = Field(description="Concise 1-sentence reason for the next action.")
    action: Literal[
        "click", "dblclick", "type", "select", "scroll", "navigate", "back", "wait", "press_key"
    ]
    element_id: int | None = Field(
        default=None,
        description="Numeric [id] from the Set-of-Marks screenshot for click, dblclick, type, or select.",
    )
    value: str | None = Field(
        default=None,
        description="Text to type, option value to select, URL to navigate, 'up'/'down' for scroll, or key name for press_key.",
    )
    suspected_visual_anomaly: bool = Field(
        default=False,
        description="Set true if screenshot shows clipped controls, broken layout, overlap, or blank screen.",
    )


class ActionPlanner(Protocol):
    async def decide(
        self,
        *,
        run_id: str,
        persona: PersonaSpec,
        som_jpeg: bytes,
        dom_state: dict[str, Any],
        history: list[str],
        step_index: int,
        max_steps: int,
    ) -> ActionDecision: ...


class GeminiActionPlanner:
    """Vision-driven navigation planner powered by Gemini Flash with automatic model & heuristic fallback."""

    def __init__(self, settings: Settings, db: Database) -> None:
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is missing. Set it in .env or pass --mock-llm for offline smoke tests.")
        self.settings = settings
        self.db = db
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self._persona_fallbacks: dict[str, HeuristicDryRunPlanner] = {}

    def _get_fallback(self, persona_name: str) -> HeuristicDryRunPlanner:
        if persona_name not in self._persona_fallbacks:
            self._persona_fallbacks[persona_name] = HeuristicDryRunPlanner()
        return self._persona_fallbacks[persona_name]

    async def decide(
        self,
        *,
        run_id: str,
        persona: PersonaSpec,
        som_jpeg: bytes,
        dom_state: dict[str, Any],
        history: list[str],
        step_index: int,
        max_steps: int,
    ) -> ActionDecision:
        compact_elements = [
            {
                "id": el["id"],
                "tag": el["tag"],
                "role": el["role"],
                "text": el["text"],
                "placeholder": el["placeholder"],
                "value": el["value"],
                "href": el["href"],
                "options": el["options"],
            }
            for el in dom_state.get("elements", [])
        ]
        prompt = (
            f"Persona goal: {persona.goal}\n"
            f"Step: {step_index}/{max_steps}\n"
            f"Current URL: {dom_state.get('url')}\n"
            f"Viewport: {dom_state.get('viewport_width')}x{dom_state.get('viewport_height')} "
            f"(scrollWidth={dom_state.get('scroll_width')})\n"
            f"Recent actions:\n" + ("\n".join(history[-6:]) if history else "(none yet)") + "\n\n"
            f"Interactive elements:\n{json.dumps(compact_elements, ensure_ascii=False)}"
        )

        async def _call_for_model(model_name: str):
            return await self.client.aio.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=som_jpeg, mime_type="image/jpeg"),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    system_instruction=(
                        f"{persona.system_prompt}\n"
                        "Choose ONE next Playwright action based on the Set-of-Marks screenshot and element list. "
                        "Avoid repeating identical failing actions."
                    ),
                    temperature=0.35,
                    response_mime_type="application/json",
                    response_schema=ActionDecision,
                ),
            )

        try:
            response, used_model = await call_gemini_with_fallback(
                self.settings.gemini_nav_model,
                DEFAULT_NAV_FALLBACKS,
                _call_for_model,
                label=f"nav:{persona.name}",
            )
        except Exception as exc:
            # Graceful fallback when API quota is exhausted across all models so real sites keep running
            fb_decision = await self._get_fallback(persona.name).decide(
                run_id=run_id,
                persona=persona,
                som_jpeg=som_jpeg,
                dom_state=dom_state,
                history=history,
                step_index=step_index,
                max_steps=max_steps,
            )
            fb_decision.thought = f"[Gemini quota fallback: {str(exc)[:45]}] {fb_decision.thought}"
            return fb_decision

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            p_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
            o_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
            cost = estimate_cost_usd(used_model, p_tok, o_tok)
            await self.db.record_llm_usage(
                run_id=run_id,
                component="nav",
                model=used_model,
                prompt_tokens=p_tok,
                output_tokens=o_tok,
                cost_usd=cost,
            )

        if response.parsed is not None and isinstance(response.parsed, ActionDecision):
            return response.parsed
        return ActionDecision.model_validate_json(response.text or "{}")


class HeuristicDryRunPlanner:
    """Deterministic offline exploratory planner used ONLY when --mock-llm is explicitly requested."""

    def __init__(self) -> None:
        self._seen_controls: set[str] = set()
        self._seen_inputs: dict[str, int] = {}
        self._visited_paths: set[str] = set()
        self._known_routes: set[str] = set()
        self._steps_on_current_path: int = 0
        self._last_path: str = ""
        self._pending_submit_after_input: bool = False

    def _next_fuzz_value(self, el: dict[str, Any], attempt_idx: int) -> str:
        el_type = (el.get("type") or "").lower()
        tag = el.get("tag", "")
        hint = " ".join(
            str(x or "")
            for x in (
                el.get("placeholder"),
                el["locator"].get("element_id"),
                el["locator"].get("name"),
                el.get("text"),
            )
        ).lower()

        if el_type == "number" or "qty" in hint:
            return ["0", "-5", "2"][attempt_idx % 3]
        if tag == "textarea" or "bio" in hint or "message" in hint:
            return "A" * 240
        if "email" in hint or el_type == "email":
            return "   "
        if "coupon" in hint or "promo" in hint:
            return "SAVE50"
        if "zip" in hint:
            return ["abc", "94107"][attempt_idx % 2]
        if "exp" in hint:
            return ["01/19", "12/28"][attempt_idx % 2]
        if "name" in hint or "address" in hint:
            return "Alex QA Tester"
        return '<img src=x onerror="window.__qa_xss=true">'

    async def decide(
        self,
        *,
        run_id: str,
        persona: PersonaSpec,
        som_jpeg: bytes,
        dom_state: dict[str, Any],
        history: list[str],
        step_index: int,
        max_steps: int,
    ) -> ActionDecision:
        from urllib.parse import urlparse

        elements = dom_state.get("elements", [])
        url = dom_state.get("url", "")
        cur_path = urlparse(url).path or "/"
        self._visited_paths.add(cur_path)
        for el in elements:
            h = el.get("href") or ""
            if h.startswith("/"):
                self._known_routes.add(h.split("?")[0])

        if cur_path != self._last_path:
            self._last_path = cur_path
            self._steps_on_current_path = 0
            self._pending_submit_after_input = False
        self._steps_on_current_path += 1

        # Accessibility personas exercise keyboard Tab navigation + page sweeps
        if persona.name.startswith("accessibility_"):
            if self._steps_on_current_path == 1:
                return ActionDecision(
                    thought="Testing keyboard Tab focus traversal on current page",
                    action="press_key",
                    value="Tab",
                )
            for el in elements:
                href = el.get("href") or ""
                if el["tag"] == "a" and href.startswith("/") and href not in self._visited_paths:
                    return ActionDecision(
                        thought=f"Sweeping route {href} for WCAG audit",
                        action="click",
                        element_id=el["id"],
                    )

        # Persona-ordered initial route dispersion so parallel workers avoid duplicating step 1
        if step_index == 1 and cur_path == "/":
            preferred_order = {
                "impatient_user": ["/cart", "/catalog"],
                "hostile_fuzzer": ["/catalog", "/profile", "/support"],
                "confused_first_timer": ["/orders", "/catalog"],
                "mobile_viewport_user": ["/cart", "/catalog"],
                "edge_explorer": ["/profile", "/support", "/orders"],
            }.get(persona.name, ["/catalog"])
            for target_route in preferred_order:
                for el in elements:
                    if el.get("href") == target_route:
                        self._seen_controls.add(f"{cur_path}:{el['locator']['primary_selector']}")
                        return ActionDecision(
                            thought=f"Dispersing persona to {target_route}",
                            action="click",
                            element_id=el["id"],
                        )

        # After typing into a form field, immediately click a non-nav action button on the page
        if self._pending_submit_after_input:
            self._pending_submit_after_input = False
            for el in elements:
                if el["tag"] == "button":
                    key = f"{cur_path}:{el['locator']['primary_selector']}"
                    txt = (el.get("text") or "").lower()
                    if key not in self._seen_controls or any(
                        w in txt for w in ("search", "apply", "save", "subscribe", "submit", "continue", "place")
                    ):
                        self._seen_controls.add(key)
                        act: Literal["click", "dblclick"] = (
                            "dblclick"
                            if persona.name == "impatient_user" and "place order" in txt
                            else "click"
                        )
                        return ActionDecision(
                            thought=f"Submitting/activating '{el['text']}' after input",
                            action=act,
                            element_id=el["id"],
                        )

        # Impatient user completes checkout Step 1 -> Step 2 -> double-clicks Place Order
        if persona.name == "impatient_user":
            if cur_path == "/checkout" and "step=2" not in url:
                for el in elements:
                    if el["tag"] == "input":
                        key = f"{cur_path}:{el['locator']['primary_selector']}"
                        if key not in self._seen_inputs:
                            self._seen_inputs[key] = 1
                            hint = (el["locator"].get("element_id") or "").lower()
                            val = "94107" if "zip" in hint else ("12/28" if "exp" in hint else "Alex Rivera")
                            self._pending_submit_after_input = True
                            return ActionDecision(
                                thought=f"Filling checkout field {hint}",
                                action="type",
                                element_id=el["id"],
                                value=val,
                            )
            for el in elements:
                if el["tag"] == "button" and "place order" in (el.get("text") or "").lower():
                    key = f"{url}:{el['locator']['primary_selector']}"
                    if key not in self._seen_controls:
                        self._seen_controls.add(key)
                        return ActionDecision(
                            thought="Impatiently double-clicking Place Order",
                            action="dblclick",
                            element_id=el["id"],
                        )

        # Hostile fuzzer tests inputs with edge/XSS/overflow payloads
        if persona.name == "hostile_fuzzer":
            for el in elements:
                if el["tag"] in {"input", "textarea"}:
                    key = f"{cur_path}:{el['locator']['primary_selector']}"
                    attempt = self._seen_inputs.get(key, 0)
                    max_attempts = 2 if (el.get("type") == "number" or "qty" in key) else 1
                    if attempt < max_attempts:
                        self._seen_inputs[key] = attempt + 1
                        val = self._next_fuzz_value(el, attempt)
                        self._pending_submit_after_input = True
                        return ActionDecision(
                            thought=f"Fuzzing input ({key}) attempt #{attempt + 1}",
                            action="type",
                            element_id=el["id"],
                            value=val,
                        )

        # Exercise dropdown <select> options (confused_first_timer / edge_explorer)
        for el in elements:
            if el["tag"] == "select" and el.get("options"):
                key = f"{cur_path}:{el['locator']['primary_selector']}"
                if key not in self._seen_controls:
                    self._seen_controls.add(key)
                    return ActionDecision(
                        thought="Testing select dropdown edge option",
                        action="select",
                        element_id=el["id"],
                        value=el["options"][-1]["value"],
                    )

        # Click untested local page buttons (before navigating away)
        for el in elements:
            if el["tag"] == "button":
                key = f"{cur_path}:{el['locator']['primary_selector']}"
                if key not in self._seen_controls:
                    self._seen_controls.add(key)
                    act = (
                        "dblclick"
                        if persona.name == "impatient_user"
                        else "click"
                    )
                    return ActionDecision(
                        thought=f"Testing button '{el['text'] or el['tag']}'",
                        action=act,
                        element_id=el["id"],
                    )

        # Navigate to an unvisited internal route (including footer links like /legal/...)
        for el in elements:
            href = el.get("href") or ""
            if el["tag"] == "a" and href.startswith("/") and href not in self._visited_paths:
                key = f"{cur_path}:{el['locator']['primary_selector']}"
                if key not in self._seen_controls:
                    self._seen_controls.add(key)
                    return ActionDecision(
                        thought=f"Exploring unvisited route {href}",
                        action="click",
                        element_id=el["id"],
                    )

        # Test direct deep-step URL ONLY when the target app actually exposes a /checkout flow
        if (
            persona.name in {"confused_first_timer", "edge_explorer"}
            and "/checkout" in self._known_routes
            and "checkout_step2_probed" not in self._visited_paths
        ):
            self._visited_paths.add("checkout_step2_probed")
            base = url.split(cur_path)[0] if cur_path != "/" else url.rstrip("/")
            return ActionDecision(
                thought="Testing direct deep-link navigation to discovered /checkout?step=2 flow",
                action="navigate",
                value=f"{base}/checkout?step=2",
            )

        return ActionDecision(thought="Scrolling to inspect remaining elements", action="scroll", value="down")


@dataclass
class WorkerStepRecord:
    step_index: int
    action: str
    url: str
    thought: str
    duration_ms: int
    console_errors: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    network_failures: list[dict[str, Any]] = field(default_factory=list)
    xss_triggered: bool = False


@dataclass
class WorkerResult:
    worker_id: str
    persona: str
    steps_executed: int
    steps: list[WorkerStepRecord]


async def _record_detected_suspects(
    *,
    db: Database,
    suspect_queue: SuspectQueue | None,
    event_bus: EventBus | None,
    run_id: str,
    worker_id: str,
    persona_name: str,
    trigger_step_index: int,
    trace_slice: list[dict[str, Any]],
    detected: list[DetectedSuspect],
    screenshot_rel: str,
    seen_signatures: set[str],
    sig_lock: asyncio.Lock | None = None,
    max_run_suspects: int = 30,
) -> int:
    count = 0
    for item in detected:
        if sig_lock is not None:
            async with sig_lock:
                if len(seen_signatures) >= max_run_suspects or item.error_signature in seen_signatures:
                    continue
                seen_signatures.add(item.error_signature)
        else:
            if len(seen_signatures) >= max_run_suspects or item.error_signature in seen_signatures:
                continue
            seen_signatures.add(item.error_signature)
        evidence = {**item.evidence, "screenshot_path": screenshot_rel}
        suspect_id = await db.create_suspect(
            run_id=run_id,
            worker_id=worker_id,
            persona=persona_name,
            category=item.category,
            error_signature=item.error_signature,
            title_hint=item.title_hint,
            url=item.url,
            trigger_step_index=trigger_step_index,
            trace_slice=trace_slice,
            evidence=evidence,
            console_logs=item.console_logs,
            network_logs=item.network_logs,
        )
        count += 1
        if event_bus is not None:
            await event_bus.publish(
                run_id,
                "suspect_detected",
                {
                    "suspect_id": suspect_id,
                    "worker_id": worker_id,
                    "persona": persona_name,
                    "category": item.category,
                    "title_hint": item.title_hint,
                    "url": item.url,
                    "status": "Detected",
                },
            )
        if suspect_queue is not None:
            await suspect_queue.push(suspect_id, run_id)
    return count


async def run_single_worker(
    *,
    browser: Browser,
    db: Database,
    artifacts: ArtifactStore,
    planner: ActionPlanner,
    run_id: str,
    target_url: str,
    persona: PersonaSpec,
    max_steps: int,
    suspect_queue: SuspectQueue | None = None,
    visual_judge: VisualJudge | None = None,
    event_bus: EventBus | None = None,
    storage_state: dict[str, Any] | None = None,
    stop_event: asyncio.Event | None = None,
    shared_seen_sigs: set[str] | None = None,
    shared_sig_lock: asyncio.Lock | None = None,
) -> WorkerResult:
    """Run one persona worker from target_url through max_steps with detection and trace persistence."""
    viewport_label = f"{persona.viewport_width}x{persona.viewport_height}"
    worker_id = await db.create_worker(
        run_id=run_id,
        persona=persona.name,
        goal=persona.goal,
        viewport=viewport_label,
        max_steps=max_steps,
    )

    session = BrowserSession(
        browser,
        viewport_width=persona.viewport_width,
        viewport_height=persona.viewport_height,
        has_touch=persona.has_touch,
        storage_state=storage_state,
    )
    history: list[str] = []
    executed_trace: list[dict[str, Any]] = []
    step_records: list[WorkerStepRecord] = []
    seen_worker_sigs: set[str] = shared_seen_sigs if shared_seen_sigs is not None else set()

    try:
        page = await session.start()
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as nav_exc:
            if page.url == "about:blank":
                raise RuntimeError(f"Failed to load {target_url}: {nav_exc}") from nav_exc
        await session.settle()

        # Check initial load signals (e.g., mobile layout overflow or cold-load noise)
        init_health = await probe_page_health(page)
        init_events = session.drain_events()
        init_xss = init_events.xss_triggered or await session.check_xss_sentinel()
        init_clean, init_som = artifacts.step_screenshot_paths(run_id, worker_id, 0)
        init_clean_jpg, _, _ = await capture_som_perception(page, init_clean, init_som)
        init_clean_rel = artifacts.to_relative(init_clean)

        if event_bus is not None:
            await event_bus.publish(
                run_id,
                "worker_frame",
                {
                    "worker_id": worker_id,
                    "persona": persona.name,
                    "viewport": viewport_label,
                    "step_index": 0,
                    "max_steps": max_steps,
                    "url": page.url,
                    "thought": f"Loaded {target_url}",
                    "last_action": "navigate",
                    "image_url": f"/media/{init_clean_rel}",
                    "frame_b64": base64.b64encode(init_clean_jpg).decode("ascii"),
                },
            )

        init_suspects = evaluate_objective_signals(
            before=init_health,
            after=init_health,
            events=init_events,
            xss_hit=init_xss,
            action_type="navigate",
            locator_json={},
            action_error=None,
        )
        await _record_detected_suspects(
            db=db,
            suspect_queue=suspect_queue,
            event_bus=event_bus,
            run_id=run_id,
            worker_id=worker_id,
            persona_name=persona.name,
            trigger_step_index=0,
            trace_slice=[],
            detected=init_suspects,
            screenshot_rel=init_clean_rel,
            seen_signatures=seen_worker_sigs,
            sig_lock=shared_sig_lock,
        )

        prev_health = init_health
        for step_index in range(1, max_steps + 1):
            if stop_event is not None and stop_event.is_set():
                break

            t0 = time.perf_counter()
            clean_path, som_path = artifacts.step_screenshot_paths(run_id, worker_id, step_index)
            clean_jpeg, som_jpeg, dom_state = await capture_som_perception(page, clean_path, som_path)

            decision = await planner.decide(
                run_id=run_id,
                persona=persona,
                som_jpeg=som_jpeg,
                dom_state=dom_state,
                history=history,
                step_index=step_index,
                max_steps=max_steps,
            )

            locator_json: dict[str, Any] = {}
            if decision.element_id is not None:
                matched = next(
                    (e for e in dom_state.get("elements", []) if e["id"] == decision.element_id),
                    None,
                )
                if matched is not None:
                    locator_json = matched["locator"]

            action_err: str | None = None
            try:
                await session.execute_action(decision.action, locator_json, decision.value)
            except Exception as exc:
                action_err = str(exc)

            after_health = await probe_page_health(page)
            events: StepEvents = session.drain_events()
            xss_hit = events.xss_triggered or await session.check_xss_sentinel()
            duration_ms = int((time.perf_counter() - t0) * 1000)
            after_url = page.url

            # Capture fresh post-action screenshot for dashboard stream & visual evidence
            post_jpg = await page.screenshot(type="jpeg", quality=75, full_page=False)
            clean_path.write_bytes(post_jpg)
            clean_rel = artifacts.to_relative(clean_path)

            history_entry = (
                f"Step {step_index}: {decision.action}(el={decision.element_id}, val={decision.value!r}) "
                f"-> {after_url}"
            )
            if action_err:
                history_entry += f" [err: {action_err[:80]}]"
            history.append(history_entry)

            executed_trace.append(
                {
                    "step_index": step_index,
                    "action_type": decision.action,
                    "locator": locator_json,
                    "input_value": decision.value,
                    "url": after_url,
                    "screenshot_path": clean_rel,
                }
            )

            await db.insert_trace_step(
                run_id=run_id,
                worker_id=worker_id,
                step_index=step_index,
                action_type=decision.action,
                locator=locator_json,
                input_value=decision.value,
                thought=decision.thought,
                url=after_url,
                screenshot_path=clean_rel,
                som_screenshot_path=artifacts.to_relative(som_path),
                duration_ms=duration_ms,
            )
            action_summary = f"{decision.action} -> {locator_json.get('primary_selector', decision.value or '')}"
            await db.update_worker(
                worker_id,
                steps_used=step_index,
                current_url=after_url,
                last_action=action_summary,
            )

            if event_bus is not None:
                await event_bus.publish(
                    run_id,
                    "worker_frame",
                    {
                        "worker_id": worker_id,
                        "persona": persona.name,
                        "viewport": viewport_label,
                        "step_index": step_index,
                        "max_steps": max_steps,
                        "url": after_url,
                        "thought": decision.thought,
                        "last_action": action_summary,
                        "image_url": f"/media/{clean_rel}",
                        "frame_b64": base64.b64encode(post_jpg).decode("ascii"),
                    },
                )

            detected = evaluate_objective_signals(
                before=prev_health,
                after=after_health,
                events=events,
                xss_hit=xss_hit,
                action_type=decision.action,
                locator_json=locator_json,
                action_error=action_err,
            )
            if persona.name.startswith("accessibility_"):
                detected.extend(await audit_wcag_accessibility(page))
            if visual_judge is not None and (
                decision.suspected_visual_anomaly or after_health.has_layout_anomaly
            ):
                vis_suspect = await visual_judge.inspect_if_needed(
                    run_id=run_id,
                    url=after_url,
                    viewport=viewport_label,
                    screenshot_jpeg=post_jpg,
                    flagged_by_agent=decision.suspected_visual_anomaly,
                )
                if vis_suspect is not None:
                    detected.append(vis_suspect)

            await _record_detected_suspects(
                db=db,
                suspect_queue=suspect_queue,
                event_bus=event_bus,
                run_id=run_id,
                worker_id=worker_id,
                persona_name=persona.name,
                trigger_step_index=step_index,
                trace_slice=list(executed_trace),
                detected=detected,
                screenshot_rel=clean_rel,
                seen_signatures=seen_worker_sigs,
                sig_lock=shared_sig_lock,
            )

            prev_health = after_health
            step_records.append(
                WorkerStepRecord(
                    step_index=step_index,
                    action=decision.action,
                    url=after_url,
                    thought=decision.thought,
                    duration_ms=duration_ms,
                    console_errors=events.console_errors,
                    page_errors=events.page_errors,
                    network_failures=events.network_failures,
                    xss_triggered=xss_hit,
                )
            )

        await db.update_worker(worker_id, status="completed", finished=True)
    except Exception:
        await db.update_worker(worker_id, status="failed", finished=True)
        raise
    finally:
        await session.close()

    return WorkerResult(
        worker_id=worker_id,
        persona=persona.name,
        steps_executed=len(step_records),
        steps=step_records,
    )

