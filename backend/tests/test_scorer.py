from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "benchmark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from scorer import load_ground_truth, score_findings  # noqa: E402


def test_benchmark_scorer_recall_and_fp_calculation() -> None:
    gt = load_ground_truth(REPO_ROOT / "test-app" / "ground_truth.json")
    bugs = gt["bugs"]

    findings = [
        {
            "error_signature": "api_failure:/api/coupon:500:post",
            "title_hint": "HTTP 500 POST on /api/coupon SAVE50",
            "url": "http://localhost:3100/cart",
        },
        {
            "error_signature": "xss:/catalog:script_or_handler_executed",
            "title_hint": "XSS execution triggered on /catalog",
            "url": "http://localhost:3100/catalog",
        },
        {
            "error_signature": "console_error:/:analytics beacon timeout",
            "title_hint": "Analytics beacon flush timeout (transient)",
            "url": "http://localhost:3100/",
        },
    ]

    result = score_findings(findings, bugs)
    assert result["true_positives"] == 2
    assert result["false_positives"] == 1
    assert result["matched_bug_ids"] == ["BUG-04", "BUG-17"]
    assert result["false_positive_rate_pct"] == 33.3

