from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import REPO_ROOT, get_settings
from core.event_bus import EventBus
from core.orchestrator import DEFAULT_SWARM_PERSONAS, SwarmOrchestrator, SwarmRunRequest
from storage.artifacts import ArtifactStore
from storage.db import Database
from workers.personas import PERSONAS

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


class LaunchRunBody(BaseModel):
    target_url: str
    mode: str = "swarm"
    personas: list[str] = Field(default_factory=lambda: list(DEFAULT_SWARM_PERSONAS))
    steps_per_worker: int = 12
    max_wall_seconds: int = 240
    github_repo: str = ""
    github_token: str = ""
    reset_url: str | None = None
    credentials: dict[str, str] | None = None
    mock_llm: bool = False


settings = get_settings()
db = Database(settings.db_path)
artifacts = ArtifactStore(settings.data_dir)
event_bus = EventBus()
orchestrator = SwarmOrchestrator(
    settings=settings,
    db=db,
    artifacts=artifacts,
    event_bus=event_bus,
)
ACTIVE_TASKS: dict[str, asyncio.Task[Any]] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.connect()
    try:
        yield
    finally:
        for task in list(ACTIVE_TASKS.values()):
            task.cancel()
        await db.close()


app = FastAPI(title="QA Swarm API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
settings.data_dir.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.data_dir), name="media")


def _compute_run_summary(
    run_row: dict[str, Any],
    workers: list[dict[str, Any]],
    suspects: list[dict[str, Any]],
    bug_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    steps_used = sum(int(w.get("steps_used") or 0) for w in workers)
    status_counts = {"Detected": 0, "Verifying": 0, "Verified": 0, "Rejected": 0}
    for s in suspects:
        st = s.get("status", "Detected")
        if st in status_counts:
            status_counts[st] += 1

    ttfvb: float | None = None
    if run_row.get("started_at") and run_row.get("first_verified_at"):
        try:
            t0 = datetime.fromisoformat(run_row["started_at"])
            t1 = datetime.fromisoformat(run_row["first_verified_at"])
            ttfvb = round(max(0.0, (t1 - t0).total_seconds()), 2)
        except Exception:
            ttfvb = None

    return {
        "steps_used": steps_used,
        "detected_total": len(suspects),
        "detecting_queue": status_counts["Detected"],
        "verifying_count": status_counts["Verifying"],
        "verified_count": status_counts["Verified"],
        "rejected_count": status_counts["Rejected"],
        "reported_count": len(bug_reports),
        "total_cost_usd": round(float(run_row.get("total_cost_usd") or 0.0), 5),
        "time_to_first_verified_sec": ttfvb,
    }


def _enrich_suspect(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "trace_slice": json.loads(row.get("trace_slice_json") or "[]"),
        "evidence": json.loads(row.get("evidence_json") or "{}"),
        "console_logs": json.loads(row.get("console_logs_json") or "[]"),
        "network_logs": json.loads(row.get("network_logs_json") or "[]"),
    }


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "gemini_configured": bool(settings.gemini_api_key and settings.gemini_api_key != "your_gemini_api_key_here"),
        "nav_model": settings.gemini_nav_model,
        "judge_model": settings.gemini_judge_model,
    }


@app.get("/api/personas")
async def list_personas() -> list[dict[str, Any]]:
    return [
        {
            "name": p.name,
            "goal": p.goal,
            "viewport": f"{p.viewport_width}x{p.viewport_height}",
            "has_touch": p.has_touch,
        }
        for p in PERSONAS.values()
    ]


@app.get("/api/runs")
async def list_runs() -> list[dict[str, Any]]:
    runs = await db.list_runs(limit=25)
    result = []
    for r in runs:
        workers = await db.list_workers(r["id"])
        suspects = await db.list_suspects(r["id"])
        bugs = await db.list_bug_reports(r["id"])
        result.append(
            {
                **r,
                "summary": _compute_run_summary(r, workers, suspects, bugs),
            }
        )
    return result


@app.post("/api/runs")
async def launch_run(body: LaunchRunBody) -> dict[str, Any]:
    req = SwarmRunRequest(
        target_url=body.target_url.strip(),
        mode=body.mode,
        personas=body.personas,
        steps_per_worker=body.steps_per_worker,
        max_wall_seconds=body.max_wall_seconds,
        github_repo=body.github_repo,
        github_token=body.github_token,
        reset_url=body.reset_url,
        credentials=body.credentials,
        mock_llm=body.mock_llm,
        headless=settings.headless,
    )
    run_id = await db.create_run(
        target_url=req.target_url,
        mode=req.mode,
        config={
            "personas": req.personas,
            "steps_per_worker": req.steps_per_worker,
            "max_wall_seconds": req.max_wall_seconds,
            "github_repo": req.github_repo,
            "mock_llm": req.mock_llm,
        },
        reset_url=req.reset_url,
    )

    async def _runner() -> None:
        try:
            await orchestrator.execute_existing_run(run_id, req)
        finally:
            ACTIVE_TASKS.pop(run_id, None)

    ACTIVE_TASKS[run_id] = asyncio.create_task(_runner())
    return {"run_id": run_id, "status": "running"}


@app.get("/api/runs/{run_id}")
async def get_run_detail(run_id: str) -> dict[str, Any]:
    run_row = await db.get_run(run_id)
    if not run_row:
        raise HTTPException(status_code=404, detail="Run not found")
    workers = await db.list_workers(run_id)
    suspects = [_enrich_suspect(s) for s in await db.list_suspects(run_id)]
    bugs = await db.list_bug_reports(run_id)
    usage = await db.list_llm_usage(run_id)

    # Attach latest screenshot URL per worker for initial load / refresh recovery
    enriched_workers = []
    for w in workers:
        steps = await db.list_trace_steps(run_id, w["id"])
        latest_shot = f"/media/{steps[-1]['screenshot_path']}" if steps else None
        enriched_workers.append({**w, "latest_image_url": latest_shot})

    return {
        "run": run_row,
        "workers": enriched_workers,
        "suspects": suspects,
        "bug_reports": bugs,
        "llm_usage": usage,
        "summary": _compute_run_summary(run_row, workers, suspects, bugs),
    }


@app.get("/api/bugs/{bug_id}")
async def get_bug_detail(bug_id: str) -> dict[str, Any]:
    bug = await db.get_bug_report(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail="Bug report not found")
    suspect = await db.get_suspect(bug["primary_suspect_id"])
    attempts = await db.list_verification_attempts(bug["primary_suspect_id"])
    return {
        "bug": {
            **bug,
            "video_url": f"/media/{bug['video_path']}" if bug.get("video_path") else None,
        },
        "suspect": _enrich_suspect(suspect) if suspect else None,
        "verification_attempts": attempts,
    }


@app.get("/api/bugs/{bug_id}/export")
async def export_bug_markdown(bug_id: str) -> PlainTextResponse:
    bug = await db.get_bug_report(bug_id)
    if not bug:
        raise HTTPException(status_code=404, detail="Bug report not found")
    return PlainTextResponse(
        content=bug["body_markdown"],
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{bug_id}.md"'},
    )


@app.get("/api/benchmarks")
async def get_benchmarks() -> dict[str, Any]:
    latest = REPO_ROOT / "benchmark" / "results" / "latest.json"
    if latest.exists():
        return json.loads(latest.read_text(encoding="utf-8"))
    return {"available": False}


@app.post("/api/demo/replay")
async def launch_demo_replay() -> dict[str, Any]:
    """Replay the most recent completed swarm run events (or a seeded local demo) cleanly over WebSocket."""
    runs = await db.list_runs(limit=15)
    source_run = next((r for r in runs if r["status"] == "completed" and r["mode"] == "swarm"), None)
    if source_run is not None:
        return {"run_id": source_run["id"], "replayed_existing": True}

    # Fall back to launching a quick local mock swarm run against NovaCart if reachable
    req = SwarmRunRequest(
        target_url="http://127.0.0.1:3100",
        mode="swarm",
        personas=list(DEFAULT_SWARM_PERSONAS),
        steps_per_worker=6,
        max_wall_seconds=90,
        mock_llm=True,
        headless=True,
    )
    run_id = await db.create_run(
        target_url=req.target_url,
        mode=req.mode,
        config={"personas": req.personas, "steps_per_worker": req.steps_per_worker, "mock_llm": True},
    )

    async def _runner() -> None:
        try:
            await orchestrator.execute_existing_run(run_id, req)
        finally:
            ACTIVE_TASKS.pop(run_id, None)

    ACTIVE_TASKS[run_id] = asyncio.create_task(_runner())
    return {"run_id": run_id, "replayed_existing": False}


@app.websocket("/ws/runs/{run_id}")
async def ws_run_stream(websocket: WebSocket, run_id: str) -> None:
    await websocket.accept()
    q = event_bus.subscribe(run_id)
    try:
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(run_id, q)

