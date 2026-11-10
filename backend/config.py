from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Approximate per-1M token rates (USD) for transparent cost telemetry.
MODEL_PRICING_PER_1M: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}


def estimate_cost_usd(model: str, prompt_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for a Gemini API call based on token counts."""
    normalized = model.lower()
    in_rate, out_rate = (0.30, 2.50)
    for key, rates in MODEL_PRICING_PER_1M.items():
        if key in normalized:
            in_rate, out_rate = rates
            break
    return (prompt_tokens * in_rate + output_tokens * out_rate) / 1_000_000.0


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_nav_model: str
    gemini_judge_model: str
    gemini_report_model: str
    gemini_embed_model: str
    github_token: str
    github_default_repo: str
    headless: bool
    max_steps: int
    max_wall_seconds: int
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "qa_swarm.db"


def get_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env", override=False)
    raw_data_dir = os.getenv("QA_SWARM_DATA_DIR", "./data")
    data_dir = Path(raw_data_dir)
    if not data_dir.is_absolute():
        data_dir = (REPO_ROOT / data_dir).resolve()

    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_nav_model=os.getenv("GEMINI_NAV_MODEL", "gemini-2.5-flash").strip(),
        gemini_judge_model=os.getenv("GEMINI_JUDGE_MODEL", "gemini-2.5-pro").strip(),
        gemini_report_model=os.getenv("GEMINI_REPORT_MODEL", "gemini-2.5-pro").strip(),
        gemini_embed_model=os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001").strip(),
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        github_default_repo=os.getenv("GITHUB_DEFAULT_REPO", "").strip(),
        headless=_parse_bool(os.getenv("QA_SWARM_HEADLESS"), True),
        max_steps=int(os.getenv("QA_SWARM_MAX_STEPS", "20")),
        max_wall_seconds=int(os.getenv("QA_SWARM_MAX_WALL_SECONDS", "240")),
        data_dir=data_dir,
    )

