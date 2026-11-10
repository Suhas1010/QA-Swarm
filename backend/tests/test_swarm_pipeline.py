from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from config import Settings
from core.event_bus import EventBus
from core.orchestrator import DEFAULT_SWARM_PERSONAS, SwarmOrchestrator, SwarmRunRequest
from detector.signatures import make_signature
from detector.visual_judge import VisualJudge
from storage.artifacts import ArtifactStore
from storage.db import Database
from verifier.replayer import ReplayVerifier

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_full_swarm_verifier_dedupe_and_markdown_export(tmp_path: Path) -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async def _run() -> None:
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=str(REPO_ROOT / "test-app"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            for _ in range(40):
                try:
                    if httpx.get(f"{base_url}/api/bootstrap", timeout=1.0).status_code == 200:
                        break
                except Exception:
                    time.sleep(0.2)

            settings = Settings(
                gemini_api_key="",
                gemini_nav_model="gemini-2.5-flash",
                gemini_judge_model="gemini-2.5-pro",
                gemini_report_model="gemini-2.5-pro",
                gemini_embed_model="gemini-embedding-001",
                github_token="",
                github_default_repo="",
                headless=True,
                max_steps=6,
                max_wall_seconds=120,
                data_dir=tmp_path,
            )
            db = Database(tmp_path / "swarm_e2e.db")
            artifacts = ArtifactStore(tmp_path)
            bus = EventBus()
            await db.connect()

            try:
                orch = SwarmOrchestrator(settings=settings, db=db, artifacts=artifacts, event_bus=bus)
                run_id = await orch.execute_run(
                    SwarmRunRequest(
                        target_url=base_url,
                        mode="swarm",
                        personas=list(DEFAULT_SWARM_PERSONAS),
                        steps_per_worker=5,
                        max_wall_seconds=90,
                        mock_llm=True,
                        headless=True,
                    )
                )

                run_row = await db.get_run(run_id)
                workers = await db.list_workers(run_id)
                suspects = await db.list_suspects(run_id)
                reports = await db.list_bug_reports(run_id)

                assert run_row is not None and run_row["status"] == "completed"
                assert len(workers) == 5
                assert len(suspects) >= 2
                verified = [s for s in suspects if s["status"] == "Verified"]
                assert len(verified) >= 1, f"Expected >=1 verified bug, got {suspects}"
                assert len(reports) >= 1

                # Every filed report must have Markdown export and verification recording
                first_report = reports[0]
                assert first_report["markdown_export_path"]
                assert artifacts.resolve(first_report["markdown_export_path"]).exists()
                assert first_report["video_path"]
                assert artifacts.resolve(first_report["video_path"]).stat().st_size > 0
                attempts = await db.list_verification_attempts(first_report["primary_suspect_id"])
                assert len(attempts) >= 2
                assert sum(a["reproduced"] for a in attempts) >= 2
            finally:
                await db.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    asyncio.run(_run())


def test_verifier_rejects_non_reproducible_false_positive(tmp_path: Path) -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async def _run() -> None:
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=str(REPO_ROOT / "test-app"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            for _ in range(40):
                try:
                    if httpx.get(f"{base_url}/api/bootstrap", timeout=1.0).status_code == 200:
                        break
                except Exception:
                    time.sleep(0.2)

            from playwright.async_api import async_playwright

            settings = Settings(
                gemini_api_key="",
                gemini_nav_model="gemini-2.5-flash",
                gemini_judge_model="gemini-2.5-pro",
                gemini_report_model="gemini-2.5-pro",
                gemini_embed_model="gemini-embedding-001",
                github_token="",
                github_default_repo="",
                headless=True,
                max_steps=5,
                max_wall_seconds=60,
                data_dir=tmp_path,
            )
            db = Database(tmp_path / "fp.db")
            artifacts = ArtifactStore(tmp_path)
            bus = EventBus()
            await db.connect()

            try:
                run_id = await db.create_run(base_url, mode="swarm")
                worker_id = await db.create_worker(run_id, "confused_first_timer", "Explore", "1280x800", 5)
                # Inject a phantom/non-reproducible suspect signature (like an LLM hallucination)
                suspect_id = await db.create_suspect(
                    run_id=run_id,
                    worker_id=worker_id,
                    persona="confused_first_timer",
                    category="js_error",
                    error_signature=make_signature("js_error", base_url, "phantom_nonexistent_error_xyz"),
                    title_hint="Phantom non-reproducible error",
                    url=base_url,
                    trigger_step_index=0,
                    trace_slice=[],
                    evidence={},
                    console_logs=[],
                    network_logs=[],
                )

                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    try:
                        verifier = ReplayVerifier(
                            browser=browser,
                            db=db,
                            artifacts=artifacts,
                            visual_judge=VisualJudge(settings, db, mock_mode=True),
                            event_bus=bus,
                        )
                        verified, vid = await verifier.verify_suspect(suspect_id)
                    finally:
                        await browser.close()

                assert verified is False
                assert vid is None
                sus = await db.get_suspect(suspect_id)
                assert sus is not None
                assert sus["status"] == "Rejected"
                assert sus["repro_count"] == 0
                attempts = await db.list_verification_attempts(suspect_id)
                assert len(attempts) == 2  # 2-of-3 early rejection after 2 consecutive failures
            finally:
                await db.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    asyncio.run(_run())


def test_fastapi_dashboard_api_endpoints() -> None:
    import main as api_main

    with TestClient(api_main.app) as client:
        h = client.get("/api/health")
        assert h.status_code == 200
        assert h.json()["ok"] is True

        p = client.get("/api/personas")
        assert p.status_code == 200
        names = {item["name"] for item in p.json()}
        assert {"impatient_user", "hostile_fuzzer", "confused_first_timer", "mobile_viewport_user"}.issubset(names)

        r = client.get("/api/runs")
        assert r.status_code == 200

