from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_ground_truth(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def match_finding_to_ground_truth(
    finding: dict[str, Any],
    gt_bugs: list[dict[str, Any]],
) -> str | None:
    """Match a suspect or bug report to a canonical ground-truth bug ID if hints align."""
    haystack = " ".join(
        [
            str(finding.get("error_signature") or ""),
            str(finding.get("title_hint") or finding.get("title") or ""),
            str(finding.get("url") or ""),
            str(finding.get("body_markdown") or ""),
        ]
    ).lower()

    best_id: str | None = None
    best_score = 0
    for gt in gt_bugs:
        hints = [h.lower() for h in gt.get("signature_hints", [])]
        hits = sum(1 for h in hints if h in haystack)
        route = str(gt.get("route") or "").lower()
        route_match = route == "/" or route in haystack
        if hits >= 2 or (hits >= 1 and route_match):
            score = hits + (1 if route_match else 0)
            if score > best_score:
                best_score = score
                best_id = gt["id"]
    return best_id


def score_findings(
    findings: list[dict[str, Any]],
    gt_bugs: list[dict[str, Any]],
) -> dict[str, Any]:
    matched_ids: set[str] = set()
    true_positives = 0
    false_positives = 0

    for item in findings:
        bug_id = match_finding_to_ground_truth(item, gt_bugs)
        if bug_id is not None:
            matched_ids.add(bug_id)
            true_positives += 1
        else:
            false_positives += 1

    total_filed = len(findings)
    total_seeded = len(gt_bugs)
    recall_pct = round((len(matched_ids) / total_seeded) * 100.0, 1) if total_seeded else 0.0
    fp_rate_pct = round((false_positives / total_filed) * 100.0, 1) if total_filed else 0.0

    return {
        "total_filed": total_filed,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "matched_bug_ids": sorted(matched_ids),
        "matched_bugs": len(matched_ids),
        "recall_pct": recall_pct,
        "false_positive_rate_pct": fp_rate_pct,
    }

