from __future__ import annotations

import asyncio
import json
from typing import Any

from core.event_bus import EventBus
from storage.db import Database, utc_now_iso


async def stream_recorded_run(
    *,
    source_run_id: str,
    target_run_id: str,
    db: Database,
    event_bus: EventBus,
) -> None:
    """Replay a previously recorded swarm run step-by-step into target_run_id for reliable demos."""
    workers = await db.list_workers(source_run_id)
    steps = await db.list_trace_steps(source_run_id)
    suspects = await db.list_suspects(source_run_id)
    reports = await db.list_bug_reports(source_run_id)

    worker_map: dict[str, str] = {}
    worker_meta: dict[str, dict[str, Any]] = {}
    for w in workers:
        new_wid = await db.create_worker(
            run_id=target_run_id,
            persona=w["persona"],
            goal=w["goal"],
            viewport=w["viewport"],
            max_steps=w["max_steps"],
        )
        worker_map[w["id"]] = new_wid
        worker_meta[w["id"]] = w

    # Interleave trace steps across workers so all 5 tiles animate smoothly
    by_step_idx = sorted(steps, key=lambda s: (int(s["step_index"]), s["worker_id"]))
    for st in by_step_idx:
        new_wid = worker_map.get(st["worker_id"])
        meta = worker_meta.get(st["worker_id"])
        if not new_wid or not meta:
            continue
        locator = json.loads(st["locator_json"] or "{}")
        await db.insert_trace_step(
            run_id=target_run_id,
            worker_id=new_wid,
            step_index=st["step_index"],
            action_type=st["action_type"],
            locator=locator,
            input_value=st["input_value"],
            thought=st["thought"],
            url=st["url"],
            screenshot_path=st["screenshot_path"],
            som_screenshot_path=st["som_screenshot_path"],
            duration_ms=st["duration_ms"],
        )
        await db.update_worker(
            new_wid,
            steps_used=st["step_index"],
            current_url=st["url"],
            last_action=st["action_type"],
        )
        await event_bus.publish(
            target_run_id,
            "worker_frame",
            {
                "worker_id": new_wid,
                "persona": meta["persona"],
                "viewport": meta["viewport"],
                "step_index": st["step_index"],
                "max_steps": meta["max_steps"],
                "url": st["url"],
                "thought": st["thought"],
                "last_action": st["action_type"],
                "image_url": f"/media/{st['screenshot_path']}",
            },
        )
        await asyncio.sleep(0.14)

    for new_wid in worker_map.values():
        await db.update_worker(new_wid, status="completed", finished=True)

    # Replay suspect lifecycle transitions: Detected -> Verifying -> Verified / Rejected
    report_by_suspect = {r["primary_suspect_id"]: r for r in reports}
    for s in suspects[:16]:
        new_wid = worker_map.get(s["worker_id"], next(iter(worker_map.values()), ""))
        if not new_wid:
            continue
        new_sid = await db.create_suspect(
            run_id=target_run_id,
            worker_id=new_wid,
            persona=s["persona"],
            category=s["category"],
            error_signature=s["error_signature"],
            title_hint=s["title_hint"],
            url=s["url"],
            trigger_step_index=s["trigger_step_index"],
            trace_slice=json.loads(s["trace_slice_json"] or "[]"),
            evidence=json.loads(s["evidence_json"] or "{}"),
            console_logs=json.loads(s["console_logs_json"] or "[]"),
            network_logs=json.loads(s["network_logs_json"] or "[]"),
        )
        await event_bus.publish(
            target_run_id,
            "suspect_detected",
            {"suspect_id": new_sid, "status": "Detected", "title_hint": s["title_hint"]},
        )
        await asyncio.sleep(0.18)

        await db.update_suspect_status(new_sid, "Verifying")
        await event_bus.publish(
            target_run_id,
            "suspect_verifying",
            {"suspect_id": new_sid, "status": "Verifying", "title_hint": s["title_hint"]},
        )
        await asyncio.sleep(0.22)

        if s["status"] == "Verified":
            await db.update_suspect_status(new_sid, "Verified", repro_count=s["repro_count"] or 2)
            await db.update_run_status(target_run_id, "running", first_verified_at=utc_now_iso())
            await event_bus.publish(
                target_run_id,
                "suspect_verified",
                {"suspect_id": new_sid, "status": "Verified", "repro_count": s["repro_count"] or 2},
            )
            rep = report_by_suspect.get(s["id"])
            if rep:
                await db.insert_bug_report(
                    run_id=target_run_id,
                    primary_suspect_id=new_sid,
                    dedupe_group_id=rep["dedupe_group_id"],
                    title=rep["title"],
                    severity=rep["severity"],
                    category=rep["category"],
                    body_markdown=rep["body_markdown"],
                    github_issue_url=rep["github_issue_url"],
                    github_issue_number=rep["github_issue_number"],
                    markdown_export_path=rep["markdown_export_path"],
                    video_path=rep["video_path"],
                )
                await event_bus.publish(target_run_id, "bug_reported", {"suspect_id": new_sid})
        else:
            await db.update_suspect_status(
                new_sid,
                "Rejected",
                repro_count=s["repro_count"] or 0,
                rejection_reason=s.get("rejection_reason") or "Failed 2/3 replay",
            )
            await event_bus.publish(
                target_run_id,
                "suspect_rejected",
                {"suspect_id": new_sid, "status": "Rejected"},
            )

    await db.update_run_status(target_run_id, "completed", finished=True)
    await event_bus.publish(target_run_id, "run_completed", {"run_id": target_run_id, "status": "completed"})

