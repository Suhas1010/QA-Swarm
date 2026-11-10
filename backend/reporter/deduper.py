from __future__ import annotations

import math
from typing import Any

from google import genai

from config import Settings
from core.llm_retry import call_gemini_with_retry
from detector.signatures import normalize_url_path


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _token_jaccard(a: str, b: str) -> float:
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class BugDeduplicator:
    """Deduplicates verified bugs by normalized signature or URL + symptom similarity."""

    def __init__(self, settings: Settings, *, mock_mode: bool = False) -> None:
        self.settings = settings
        self.mock_mode = mock_mode or not bool(settings.gemini_api_key)
        self.client = None if self.mock_mode else genai.Client(api_key=settings.gemini_api_key)
        self._seen_by_run: dict[str, list[dict[str, Any]]] = {}

    async def _embed(self, text: str) -> list[float] | None:
        if self.mock_mode or self.client is None:
            return None
        try:
            async def _call():
                return await self.client.aio.models.embed_content(
                    model=self.settings.gemini_embed_model,
                    contents=text[:500],
                )

            res = await call_gemini_with_retry(_call, max_retries=2, label="embed")
            embeddings = getattr(res, "embeddings", None) or []
            if embeddings:
                return list(embeddings[0].values)
        except Exception:
            return None
        return None

    async def check_duplicate(
        self,
        *,
        run_id: str,
        signature: str,
        url: str,
        title_hint: str,
        category: str,
    ) -> str | None:
        """Return existing dedupe_group_id if duplicate, else None."""
        seen = self._seen_by_run.setdefault(run_id, [])
        norm_path = normalize_url_path(url)
        sig_key = signature.strip().lower()

        for item in seen:
            if item["signature"] == sig_key:
                return item["dedupe_group_id"]

        candidate_vec = await self._embed(f"{category} {norm_path} {title_hint}")
        for item in seen:
            if item["path"] != norm_path or item["category"] != category:
                continue
            if candidate_vec is not None and item["embedding"] is not None:
                if _cosine(candidate_vec, item["embedding"]) >= 0.85:
                    return item["dedupe_group_id"]
            elif _token_jaccard(title_hint, item["title_hint"]) >= 0.72:
                return item["dedupe_group_id"]

        return None

    async def register(
        self,
        *,
        run_id: str,
        dedupe_group_id: str,
        signature: str,
        url: str,
        title_hint: str,
        category: str,
    ) -> None:
        vec = await self._embed(f"{category} {normalize_url_path(url)} {title_hint}")
        self._seen_by_run.setdefault(run_id, []).append(
            {
                "dedupe_group_id": dedupe_group_id,
                "signature": signature.strip().lower(),
                "path": normalize_url_path(url),
                "category": category,
                "title_hint": title_hint,
                "embedding": vec,
            }
        )

