from __future__ import annotations

import json
from typing import Any, Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from config import Settings, estimate_cost_usd
from core.llm_retry import call_gemini_with_retry
from storage.db import Database


class SynthesizedBugReport(BaseModel):
    title: str = Field(description="Clear, concise GitHub issue title (<90 chars)")
    severity: Literal["Critical", "High", "Medium", "Low"]
    expected_behavior: str
    actual_behavior: str


def _default_severity(category: str) -> str:
    if category in {"xss", "broken_flow"}:
        return "Critical"
    if category in {"api_failure", "js_error"}:
        return "High"
    return "Medium"


def _format_repro_steps(trace_slice: list[dict[str, Any]], start_url: str) -> list[str]:
    steps = [f"Navigate to `{start_url}`."]
    for s in trace_slice:
        act = s.get("action_type", "action")
        loc = (s.get("locator") or {}).get("primary_selector") or (s.get("locator") or {}).get("role_name") or ""
        val = s.get("input_value")
        desc = f"`{act}`"
        if loc:
            desc += f" on `{loc}`"
        if val is not None and val != "":
            preview = val if len(val) <= 80 else val[:77] + "..."
            desc += f" with value `{preview}`"
        steps.append(f"{desc} (at `{s.get('url', '')}`)")
    return steps


class ReportWriter:
    """Generates clean, developer-ready bug reports from verified replay traces."""

    def __init__(self, settings: Settings, db: Database, *, mock_mode: bool = False) -> None:
        self.settings = settings
        self.db = db
        self.mock_mode = mock_mode or not bool(settings.gemini_api_key)
        self.client = None if self.mock_mode else genai.Client(api_key=settings.gemini_api_key)

    async def write_report(
        self,
        *,
        run_id: str,
        suspect: dict[str, Any],
        target_url: str,
        repro_count: int,
        video_rel_path: str | None,
    ) -> dict[str, str]:
        trace_slice: list[dict[str, Any]] = json.loads(suspect["trace_slice_json"] or "[]")
        console_logs = json.loads(suspect["console_logs_json"] or "[]")
        network_logs = json.loads(suspect["network_logs_json"] or "[]")
        repro_steps = _format_repro_steps(trace_slice, target_url)

        title = suspect["title_hint"][:90]
        severity = _default_severity(suspect["category"])
        expected = "The application should complete the user action cleanly without unhandled errors, failed server responses, or broken layout."
        actual = suspect["title_hint"]

        if not self.mock_mode and self.client is not None:
            prompt = (
                f"Category: {suspect['category']}\n"
                f"Persona: {suspect['persona']}\n"
                f"URL: {suspect['url']}\n"
                f"Symptom: {suspect['title_hint']}\n"
                f"Signature: {suspect['error_signature']}\n"
                f"Repro Steps:\n" + "\n".join(f"- {x}" for x in repro_steps) + "\n"
                f"Console logs: {json.dumps(console_logs[:5])}\n"
                f"Network logs: {json.dumps(network_logs[:5])}\n"
            )
            try:
                async def _call():
                    return await self.client.aio.models.generate_content(
                        model=self.settings.gemini_report_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=(
                                "Write a crisp, high-signal developer bug report summary based strictly "
                                "on the observed trace and logs. Do not invent steps or metrics."
                            ),
                            temperature=0.2,
                            response_mime_type="application/json",
                            response_schema=SynthesizedBugReport,
                        ),
                    )

                response = await call_gemini_with_retry(_call, label="report")
                usage = getattr(response, "usage_metadata", None)
                if usage is not None:
                    p_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
                    o_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
                    await self.db.record_llm_usage(
                        run_id=run_id,
                        component="report",
                        model=self.settings.gemini_report_model,
                        prompt_tokens=p_tok,
                        output_tokens=o_tok,
                        cost_usd=estimate_cost_usd(self.settings.gemini_report_model, p_tok, o_tok),
                    )
                parsed = (
                    response.parsed
                    if isinstance(response.parsed, SynthesizedBugReport)
                    else SynthesizedBugReport.model_validate_json(response.text or "{}")
                )
                title = parsed.title[:90]
                severity = parsed.severity
                expected = parsed.expected_behavior
                actual = parsed.actual_behavior
            except Exception:
                pass

        lines = [
            f"# {title}",
            "",
            f"- **Severity:** {severity}",
            f"- **Category:** `{suspect['category']}`",
            f"- **Persona:** `{suspect['persona']}`",
            f"- **Replay Verification:** Verified (`{repro_count}/3` fresh browser attempts reproduced)",
            f"- **URL:** `{suspect['url']}`",
            f"- **Error Signature:** `{suspect['error_signature']}`",
            "",
            "## Expected Behavior",
            expected,
            "",
            "## Actual Behavior",
            actual,
            "",
            "## Reproduction Steps",
        ]
        for idx, step_text in enumerate(repro_steps, 1):
            lines.append(f"{idx}. {step_text}")

        if console_logs:
            lines.extend(["", "## Console / JS Errors", "```json", json.dumps(console_logs[:5], indent=2), "```"])
        if network_logs:
            lines.extend(["", "## Network Failures", "```json", json.dumps(network_logs[:5], indent=2), "```"])
        if video_rel_path:
            lines.extend(["", "## Verification Recording", f"Saved video artifact: `{video_rel_path}`"])

        return {
            "title": title,
            "severity": severity,
            "body_markdown": "\n".join(lines),
        }

