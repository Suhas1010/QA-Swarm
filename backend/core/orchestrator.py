from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Browser, async_playwright

from config import Settings
from core.event_bus import EventBus
from core.queue import SuspectQueue
from detector.visual_judge import VisualJudge
from reporter.deduper import BugDeduplicator
from reporter.github_client import GitHubIssueClient
from reporter.report_writer import ReportWriter
from storage.artifacts import ArtifactStore
from storage.db import Database
from verifier.replayer import ReplayVerifier
from workers.agent_loop import (
    ActionPlanner,
    GeminiActionPlanner,
    HeuristicDryRunPlanner,
    run_single_worker,
)
from workers.personas import get_persona

DEFAULT_SWARM_PERSONAS = [
    "impatient_user",
    "hostile_fuzzer",
    "confused_first_timer",
    "mobile_viewport_user",
    "edge_explorer",
]


@dataclass
class SwarmRunRequest:
    target_url: str
    mode: str = "swarm"  # "swarm" | "single"
    personas: list[str] = field(default_factory=lambda: list(DEFAULT_SWARM_PERSONAS))
    steps_per_worker: int = 20
    max_wall_seconds: int = 240
    github_repo: str = ""
    github_token: str = ""
    reset_url: str | None = None
    credentials: dict[str, str] | None = None
    mock_llm: bool = False
    headless: bool = True


async def _capture_login_storage_state(
    browser: Browser,
    target_url: str,
    credentials: dict[str, str],
) -> dict[str, Any] | None:
    """Optionally authenticate once before starting workers so all contexts share session state."""
    email = (credentials.get("email") or "").strip()
    password = (credentials.get("password") or "").strip()
    if not email or not password:
        return None
    login_url = (credentials.get("login_url") or f"{target_url.rstrip('/')}/login").strip()

    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        page = await ctx.new_page()
        await page.goto(login_url, wait_until="domcontentloaded", timeout=15000)
        email_loc = page.locator('input[type="email"], input[name*="email" i], #login-email').first
        pw_loc = page.locator('input[type="password"], #login-password').first
        btn_loc = page.locator('button[type="submit"], #login-submit-btn, button:has-text("Sign In"), button:has-text("Login")').first
        if await email_loc.count() > 0 and await pw_loc.count() > 0:
            await email_loc.fill(email)
            await pw_loc.fill(password)
            if await btn_loc.count() > 0:
                await btn_loc.click()
                await asyncio.sleep(0.8)
        return await ctx.storage_state()
    except Exception:
        return None
    finally:
        await ctx.close()


class SwarmOrchestrator:
    """Coordinates parallel persona workers, suspect queue, replay verifier, and reporter."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        artifacts: ArtifactStore,
        event_bus: EventBus,
    ) -> None:
        self.settings = settings
        self.db = db
        self.artifacts = artifacts
        self.event_bus = event_bus

    def _build_worker_specs(self, req: SwarmRunRequest) -> list[tuple[str, int]]:
        if req.mode == "single":
            total_budget = max(1, req.steps_per_worker * max(1, len(req.personas) or 5))
            return [("edge_explorer", total_budget)]

        chosen = [p for p in req.personas if p]
        if not chosen:
            chosen = list(DEFAULT_SWARM_PERSONAS)
        # Ensure up to 5 concurrent workers as specified in FR1
        while len(chosen) < 5:
            for extra in DEFAULT_SWARM_PERSONAS:
                if extra not in chosen:
                    chosen.append(extra)
                    break
            else:
                chosen.append("confused_first_timer")
        return [(p, req.steps_per_worker) for p in chosen[:5]]

    async def execute_run(self, req: SwarmRunRequest, *, run_id: str | None = None) -> str:
        rid = await self.db.create_run(
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
            run_id=run_id,
        )
        await self.execute_existing_run(rid, req)
        return rid

    async def execute_existing_run(self, rid: str, req: SwarmRunRequest) -> None:
        worker_specs = self._build_worker_specs(req)
        suspect_queue = SuspectQueue()
        stop_event = asyncio.Event()

        use_mock = req.mock_llm or not bool(self.settings.gemini_api_key)
        planner: ActionPlanner = (
            HeuristicDryRunPlanner()
            if use_mock
            else GeminiActionPlanner(self.settings, self.db)
        )
        visual_judge = VisualJudge(self.settings, self.db, mock_mode=use_mock)
        deduper = BugDeduplicator(self.settings, mock_mode=use_mock)
        writer = ReportWriter(self.settings, self.db, mock_mode=use_mock)
        gh_client = GitHubIssueClient(
            token=req.github_token or self.settings.github_token,
            repo=req.github_repo or self.settings.github_default_repo,
            artifacts=self.artifacts,
        )

        async def _wall_clock_guard() -> None:
            await asyncio.sleep(max(10, req.max_wall_seconds))
            stop_event.set()

        guard_task = asyncio.create_task(_wall_clock_guard())

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=req.headless)
                try:
                    storage_state = (
                        await _capture_login_storage_state(browser, req.target_url, req.credentials)
                        if req.credentials
                        else None
                    )

                    verifier = ReplayVerifier(
                        browser=browser,
                        db=self.db,
                        artifacts=self.artifacts,
                        visual_judge=visual_judge,
                        event_bus=self.event_bus,
                    )

                    verified_sigs: dict[str, int] = {}
                    sig_lock = asyncio.Lock()
                    report_lock = asyncio.Lock()

                    async def _verifier_consumer() -> None:
                        while True:
                            job = await suspect_queue.pop()
                            if job is None:
                                suspect_queue.task_done()
                                break
                            try:
                                suspect_pre = await self.db.get_suspect(job.suspect_id)
                                if not suspect_pre:
                                    continue
                                sig = suspect_pre["error_signature"]
                                async with sig_lock:
                                    cached_repro = verified_sigs.get(sig)
                                if cached_repro is not None:
                                    await self.db.update_suspect_status(
                                        job.suspect_id, "Verified", repro_count=cached_repro
                                    )
                                    await self.event_bus.publish(
                                        rid,
                                        "suspect_verified",
                                        {
                                            "suspect_id": job.suspect_id,
                                            "status": "Verified",
                                            "repro_count": cached_repro,
                                            "title_hint": suspect_pre["title_hint"],
                                            "category": suspect_pre["category"],
                                            "persona": suspect_pre["persona"],
                                            "url": suspect_pre["url"],
                                        },
                                    )
                                    continue

                                verified, vid_rel = await verifier.verify_suspect(job.suspect_id)
                                if verified:
                                    suspect = await self.db.get_suspect(job.suspect_id)
                                    if suspect:
                                        async with sig_lock:
                                            verified_sigs[sig] = suspect["repro_count"] or 2
                                        async with report_lock:
                                            dup_group = await deduper.check_duplicate(
                                                run_id=rid,
                                                signature=suspect["error_signature"],
                                                url=suspect["url"],
                                                title_hint=suspect["title_hint"],
                                                category=suspect["category"],
                                            )
                                            if dup_group is None:
                                                group_id = f"grp_{uuid.uuid4().hex[:8]}"
                                                await deduper.register(
                                                    run_id=rid,
                                                    dedupe_group_id=group_id,
                                                    signature=suspect["error_signature"],
                                                    url=suspect["url"],
                                                    title_hint=suspect["title_hint"],
                                                    category=suspect["category"],
                                                )
                                                synthesized = await writer.write_report(
                                                    run_id=rid,
                                                    suspect=suspect,
                                                    target_url=req.target_url,
                                                    repro_count=suspect["repro_count"] or 2,
                                                    video_rel_path=vid_rel,
                                                )
                                                ev = json.loads(suspect["evidence_json"] or "{}")
                                                pub = await gh_client.publish_bug(
                                                    run_id=rid,
                                                    bug_key=job.suspect_id,
                                                    title=synthesized["title"],
                                                    severity=synthesized["severity"],
                                                    category=suspect["category"],
                                                    body_markdown=synthesized["body_markdown"],
                                                    video_rel_path=vid_rel,
                                                    screenshot_rel_path=ev.get("screenshot_path"),
                                                )
                                                bug_id = await self.db.insert_bug_report(
                                                    run_id=rid,
                                                    primary_suspect_id=job.suspect_id,
                                                    dedupe_group_id=group_id,
                                                    title=synthesized["title"],
                                                    severity=synthesized["severity"],
                                                    category=suspect["category"],
                                                    body_markdown=synthesized["body_markdown"],
                                                    github_issue_url=pub["github_issue_url"],
                                                    github_issue_number=pub["github_issue_number"],
                                                    markdown_export_path=pub["markdown_export_path"],
                                                    video_path=vid_rel,
                                                )
                                                await self.event_bus.publish(
                                                    rid,
                                                    "bug_reported",
                                                    {
                                                        "bug_id": bug_id,
                                                        "suspect_id": job.suspect_id,
                                                        "title": synthesized["title"],
                                                        "severity": synthesized["severity"],
                                                        "category": suspect["category"],
                                                        "github_issue_url": pub["github_issue_url"],
                                                        "markdown_export_path": pub["markdown_export_path"],
                                                        "video_path": vid_rel,
                                                    },
                                                )
                            finally:
                                suspect_queue.task_done()

                    num_verifiers = 2
                    consumer_tasks = [
                        asyncio.create_task(_verifier_consumer()) for _ in range(num_verifiers)
                    ]

                    # Each worker creates its own planner instance when mock so per-worker exploration state stays isolated
                    run_seen_sigs: set[str] = set()
                    run_sig_lock = asyncio.Lock()
                    worker_coros = []
                    for persona_name, step_cap in worker_specs:
                        w_planner: ActionPlanner = (
                            HeuristicDryRunPlanner() if use_mock else planner
                        )
                        worker_coros.append(
                            run_single_worker(
                                browser=browser,
                                db=self.db,
                                artifacts=self.artifacts,
                                planner=w_planner,
                                run_id=rid,
                                target_url=req.target_url,
                                persona=get_persona(persona_name),
                                max_steps=step_cap,
                                suspect_queue=suspect_queue,
                                visual_judge=visual_judge,
                                event_bus=self.event_bus,
                                storage_state=storage_state,
                                stop_event=stop_event,
                                shared_seen_sigs=run_seen_sigs,
                                shared_sig_lock=run_sig_lock,
                            )
                        )

                    await asyncio.gather(*worker_coros, return_exceptions=True)
                    for _ in range(num_verifiers):
                        await suspect_queue.close()
                    await asyncio.gather(*consumer_tasks)
                finally:
                    await browser.close()

            await self.db.update_run_status(rid, "completed", finished=True)
            await self.event_bus.publish(rid, "run_completed", {"run_id": rid, "status": "completed"})
        except Exception:
            await self.db.update_run_status(rid, "failed", finished=True)
            await self.event_bus.publish(rid, "run_completed", {"run_id": rid, "status": "failed"})
            raise
        finally:
            guard_task.cancel()

