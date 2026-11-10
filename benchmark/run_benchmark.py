from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import get_settings  # noqa: E402
from core.event_bus import EventBus  # noqa: E402
from core.orchestrator import DEFAULT_SWARM_PERSONAS, SwarmOrchestrator, SwarmRunRequest  # noqa: E402
from scorer import load_ground_truth, score_findings  # noqa: E402
from storage.artifacts import ArtifactStore  # noqa: E402
from storage.db import Database  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def run_benchmark_suite(*, steps_per_worker: int, mock_llm: bool) -> dict:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT / "test-app"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    target_url = f"http://127.0.0.1:{port}"

    try:
        for _ in range(40):
            try:
                if httpx.get(f"{target_url}/api/bootstrap", timeout=1.0).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)

        gt = load_ground_truth(REPO_ROOT / "test-app" / "ground_truth.json")
        gt_bugs = gt["bugs"]

        settings = get_settings()
        db = Database(settings.db_path)
        artifacts = ArtifactStore(settings.data_dir)
        bus = EventBus()
        await db.connect()

        try:
            orch = SwarmOrchestrator(settings=settings, db=db, artifacts=artifacts, event_bus=bus)

            # 1. Single-agent baseline at equal total step budget
            single_req = SwarmRunRequest(
                target_url=target_url,
                mode="single",
                personas=list(DEFAULT_SWARM_PERSONAS),
                steps_per_worker=steps_per_worker,
                max_wall_seconds=300,
                mock_llm=mock_llm,
                headless=True,
            )
            single_run_id = await orch.execute_run(single_req)
            single_workers = await db.list_workers(single_run_id)
            single_suspects = await db.list_suspects(single_run_id)
            single_run = await db.get_run(single_run_id)
            single_verified = [s for s in single_suspects if s["status"] == "Verified"]
            single_score = score_findings(single_verified, gt_bugs)

            # 2. Swarm (5 concurrent workers)
            httpx.post(f"{target_url}/__qa_reset", timeout=2.0)
            swarm_req = SwarmRunRequest(
                target_url=target_url,
                mode="swarm",
                personas=list(DEFAULT_SWARM_PERSONAS),
                steps_per_worker=steps_per_worker,
                max_wall_seconds=300,
                mock_llm=mock_llm,
                headless=True,
            )
            swarm_run_id = await orch.execute_run(swarm_req)
            swarm_workers = await db.list_workers(swarm_run_id)
            swarm_suspects = await db.list_suspects(swarm_run_id)
            swarm_reports = await db.list_bug_reports(swarm_run_id)
            swarm_run = await db.get_run(swarm_run_id)

            # Dedupe raw suspects by signature to measure "No Verifier" baseline fairly
            deduped_raw_map = {}
            for s in swarm_suspects:
                deduped_raw_map.setdefault(s["error_signature"], s)
            raw_score = score_findings(list(deduped_raw_map.values()), gt_bugs)

            verified_suspects = [s for s in swarm_suspects if s["status"] == "Verified"]
            rejected_suspects = [s for s in swarm_suspects if s["status"] == "Rejected"]
            verified_score = score_findings(swarm_reports or verified_suspects, gt_bugs)

            ttfvb: float | None = None
            if swarm_run and swarm_run.get("started_at") and swarm_run.get("first_verified_at"):
                t0 = datetime.fromisoformat(swarm_run["started_at"])
                t1 = datetime.fromisoformat(swarm_run["first_verified_at"])
                ttfvb = round(max(0.0, (t1 - t0).total_seconds()), 2)

            summary = {
                "available": True,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "mock_llm": mock_llm,
                "total_seeded_bugs": len(gt_bugs),
                "single_agent": {
                    "run_id": single_run_id,
                    "steps_used": sum(w["steps_used"] for w in single_workers),
                    "matched_bugs": single_score["matched_bugs"],
                    "recall_pct": single_score["recall_pct"],
                    "cost_usd": round(float((single_run or {}).get("total_cost_usd") or 0.0), 5),
                },
                "swarm_no_verifier": raw_score,
                "swarm_with_verifier": {
                    "run_id": swarm_run_id,
                    "steps_used": sum(w["steps_used"] for w in swarm_workers),
                    **verified_score,
                    "rejected_count": len(rejected_suspects),
                    "cost_usd": round(float((swarm_run or {}).get("total_cost_usd") or 0.0), 5),
                    "time_to_first_verified_sec": ttfvb,
                },
            }

            out_dir = REPO_ROOT / "benchmark" / "results"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "latest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return summary
        finally:
            await db.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run QA Swarm benchmark against NovaCart")
    parser.add_argument("--steps", type=int, default=12, help="Steps per worker (single gets 5x)")
    parser.add_argument("--mock-llm", action="store_true", help="Run using deterministic offline planner")
    args = parser.parse_args()

    result = asyncio.run(run_benchmark_suite(steps_per_worker=args.steps, mock_llm=args.mock_llm))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

