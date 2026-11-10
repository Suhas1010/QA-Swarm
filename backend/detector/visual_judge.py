from __future__ import annotations

from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from config import Settings, estimate_cost_usd
from core.llm_retry import DEFAULT_STRONG_FALLBACKS, call_gemini_with_fallback
from detector.objective import DetectedSuspect
from detector.signatures import make_signature, normalize_url_path
from storage.db import Database


class VisualBugJudgment(BaseModel):
    has_bug: bool = Field(description="True only if there is an unmistakable visible layout/UI defect.")
    category: Literal["layout_break", "broken_flow", "none"] = "none"
    severity: Literal["Critical", "High", "Medium", "Low"] = "Medium"
    summary: str = Field(description="Concise 1-sentence description of the visible bug.")


class VisualReplayMatch(BaseModel):
    reproduced: bool = Field(description="True if the exact same visual defect appears in the replay screenshot.")
    reason: str = Field(description="Short explanation comparing replay screenshot to the original symptom.")


class VisualJudge:
    """Stronger Gemini judge invoked selectively for visual defects and replay confirmation."""

    def __init__(self, settings: Settings, db: Database, *, mock_mode: bool = False) -> None:
        self.settings = settings
        self.db = db
        self.mock_mode = mock_mode or not bool(settings.gemini_api_key)
        self.client = None if self.mock_mode else genai.Client(api_key=settings.gemini_api_key)
        self._checked_keys: set[str] = set()

    async def inspect_if_needed(
        self,
        *,
        run_id: str,
        url: str,
        viewport: str,
        screenshot_jpeg: bytes,
        flagged_by_agent: bool,
    ) -> DetectedSuspect | None:
        if self.mock_mode or self.client is None or not flagged_by_agent:
            return None

        cache_key = f"{normalize_url_path(url)}:{viewport}"
        if cache_key in self._checked_keys:
            return None
        self._checked_keys.add(cache_key)

        async def _call(model_name: str):
            return await self.client.aio.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=screenshot_jpeg, mime_type="image/jpeg"),
                    (
                        f"Inspect this screenshot from {url} (viewport {viewport}). "
                        "Report has_bug=true ONLY if there is clear visible breakage: overlapping text/buttons, "
                        "cut-off interactive controls, broken blank render, or severe layout collapse."
                    ),
                ],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=VisualBugJudgment,
                ),
            )

        try:
            response, used_model = await call_gemini_with_fallback(
                self.settings.gemini_judge_model,
                DEFAULT_STRONG_FALLBACKS,
                _call,
                label="judge:visual",
            )
        except Exception:
            return None

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            p_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
            o_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
            await self.db.record_llm_usage(
                run_id=run_id,
                component="judge",
                model=used_model,
                prompt_tokens=p_tok,
                output_tokens=o_tok,
                cost_usd=estimate_cost_usd(used_model, p_tok, o_tok),
            )

        judgment = (
            response.parsed
            if isinstance(response.parsed, VisualBugJudgment)
            else VisualBugJudgment.model_validate_json(response.text or "{}")
        )
        if not judgment.has_bug or judgment.category == "none":
            return None

        return DetectedSuspect(
            category=judgment.category,
            error_signature=make_signature("visual", url, f"{viewport}:{judgment.summary[:60]}"),
            title_hint=judgment.summary,
            url=url,
            evidence={"visual_summary": judgment.summary, "severity_hint": judgment.severity},
            is_visual=True,
        )

    async def verify_replay_screenshot(
        self,
        *,
        run_id: str,
        symptom: str,
        original_jpeg: bytes | None,
        replay_jpeg: bytes,
    ) -> tuple[bool, str]:
        if self.mock_mode or self.client is None:
            return True, "Offline/heuristic visual match"

        parts: list[Any] = []
        if original_jpeg:
            parts.append(types.Part.from_bytes(data=original_jpeg, mime_type="image/jpeg"))
        parts.append(types.Part.from_bytes(data=replay_jpeg, mime_type="image/jpeg"))
        parts.append(
            f"Suspected visual bug: '{symptom}'. "
            "Does the replay screenshot (last image) reproduce the exact same visual defect?"
        )

        async def _call(model_name: str):
            return await self.client.aio.models.generate_content(
                model=model_name,
                contents=parts,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=VisualReplayMatch,
                ),
            )

        try:
            response, used_model = await call_gemini_with_fallback(
                self.settings.gemini_judge_model,
                DEFAULT_STRONG_FALLBACKS,
                _call,
                label="judge:replay",
            )
        except Exception:
            return True, "Visual judge fallback match (quota unavailable)"

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            p_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
            o_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
            await self.db.record_llm_usage(
                run_id=run_id,
                component="judge",
                model=used_model,
                prompt_tokens=p_tok,
                output_tokens=o_tok,
                cost_usd=estimate_cost_usd(used_model, p_tok, o_tok),
            )

        res = (
            response.parsed
            if isinstance(response.parsed, VisualReplayMatch)
            else VisualReplayMatch.model_validate_json(response.text or "{}")
        )
        return res.reproduced, res.reason

