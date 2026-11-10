from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn
from playwright.async_api import async_playwright

from config import get_settings
from core.event_bus import EventBus
from core.orchestrator import DEFAULT_SWARM_PERSONAS, SwarmOrchestrator, SwarmRunRequest
from storage.artifacts import ArtifactStore
from storage.db import Database
from workers.agent_loop import (
    GeminiActionPlanner,
    HeuristicDryRunPlanner,
    run_single_worker,
)
from workers.personas import get_persona


def configure_windows_event_loop() -> None:
    """Ensure Playwright async subprocesses work reliably on Windows."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


async def _run_single_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.data_dir)
    await db.connect()

    try:
        persona = get_persona(args.persona)
        planner = (
            HeuristicDryRunPlanner()
            if args.mock_llm
            else GeminiActionPlanner(settings, db)
        )
        run_id = await db.create_run(
            target_url=args.url,
            mode="single_day1",
            config={"persona": persona.name, "steps": args.steps, "mock_llm": args.mock_llm},
        )

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=settings.headless if not args.headed else False)
                try:
                    result = await run_single_worker(
                        browser=browser,
                        db=db,
                        artifacts=artifacts,
                        planner=planner,
                        run_id=run_id,
                        target_url=args.url,
                        persona=persona,
                        max_steps=args.steps,
                    )
                finally:
                    await browser.close()
        except Exception:
            await db.update_run_status(run_id, "failed", finished=True)
            raise

        await db.update_run_status(run_id, "completed", finished=True)
        run_row = await db.get_run(run_id)
        trace_rows = await db.list_trace_steps(run_id, result.worker_id)

        print(f"Run ID: {run_id}")
        print(f"Worker ID: {result.worker_id} | Persona: {result.persona}")
        print(f"Steps executed: {result.steps_executed} | Persisted trace rows: {len(trace_rows)}")
        print(f"Measured LLM cost: ${(run_row or {}).get('total_cost_usd', 0.0):.5f}")
        for s in result.steps:
            signals = []
            if s.console_errors:
                signals.append(f"console_err={len(s.console_errors)}")
            if s.page_errors:
                signals.append(f"page_err={len(s.page_errors)}")
            if s.network_failures:
                signals.append(f"net_fail={len(s.network_failures)}")
            if s.xss_triggered:
                signals.append("XSS_TRIGGERED")
            sig_str = f" | SIGNALS: {', '.join(signals)}" if signals else ""
            print(f"  [{s.step_index:02d}] {s.action:<8} {s.url} ({s.duration_ms}ms){sig_str}")
        return 0
    finally:
        await db.close()


async def _run_swarm_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.data_dir)
    event_bus = EventBus()
    await db.connect()

    try:
        orchestrator = SwarmOrchestrator(
            settings=settings,
            db=db,
            artifacts=artifacts,
            event_bus=event_bus,
        )
        req = SwarmRunRequest(
            target_url=args.url,
            mode=args.mode,
            personas=list(DEFAULT_SWARM_PERSONAS),
            steps_per_worker=args.steps,
            max_wall_seconds=args.wall_seconds,
            github_repo=args.github_repo or "",
            mock_llm=args.mock_llm,
            headless=settings.headless if not args.headed else False,
        )
        run_id = await orchestrator.execute_run(req)
        suspects = await db.list_suspects(run_id)
        bugs = await db.list_bug_reports(run_id)
        run_row = await db.get_run(run_id)
        verified = sum(1 for s in suspects if s["status"] == "Verified")
        rejected = sum(1 for s in suspects if s["status"] == "Rejected")
        print(f"Swarm Run ID: {run_id} ({req.mode})")
        print(
            f"Detected suspects: {len(suspects)} | Verified: {verified} | Rejected: {rejected} | Filed reports: {len(bugs)}"
        )
        print(f"Total LLM cost: ${(run_row or {}).get('total_cost_usd', 0.0):.5f}")
        for b in bugs:
            dest = b.get("github_issue_url") or b.get("markdown_export_path")
            print(f"  [{b['severity']}] {b['title']} -> {dest}")
        return 0
    finally:
        await db.close()


def main() -> int:
    configure_windows_event_loop()
    parser = argparse.ArgumentParser(description="QA Swarm CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    single = sub.add_parser("run-single", help="Run a single Playwright worker agent with trace logging")
    single.add_argument("--url", required=True, help="Target URL")
    single.add_argument("--persona", default="confused_first_timer", help="Persona name")
    single.add_argument("--steps", type=int, default=8, help="Max navigation steps")
    single.add_argument("--headed", action="store_true", help="Show browser window")
    single.add_argument("--mock-llm", action="store_true", help="Offline heuristic planner")

    swarm = sub.add_parser("run-swarm", help="Run full QA Swarm pipeline (Workers -> Verifier -> Reporter)")
    swarm.add_argument("--url", required=True, help="Target URL")
    swarm.add_argument("--mode", choices=["swarm", "single"], default="swarm")
    swarm.add_argument("--steps", type=int, default=10, help="Steps per worker")
    swarm.add_argument("--wall-seconds", type=int, default=240, help="Hard wall-clock cap")
    swarm.add_argument("--github-repo", default="", help="Optional owner/repo for issue filing")
    swarm.add_argument("--headed", action="store_true", help="Show browser windows")
    swarm.add_argument("--mock-llm", action="store_true", help="Offline heuristic planner")

    serve = sub.add_parser("serve", help="Start the QA Swarm FastAPI backend server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    if args.command == "run-single":
        return asyncio.run(_run_single_async(args))
    if args.command == "run-swarm":
        return asyncio.run(_run_swarm_async(args))
    if args.command == "serve":
        uvicorn.run("main:app", host=args.host, port=args.port, loop="asyncio")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
