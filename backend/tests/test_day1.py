from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright

from config import Settings
from core.llm_retry import call_gemini_with_retry, parse_retry_delay_seconds
from storage.artifacts import ArtifactStore
from storage.db import Database
from workers.agent_loop import ActionDecision, GeminiActionPlanner, HeuristicDryRunPlanner, run_single_worker
from workers.personas import get_persona

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_database_and_artifacts_crud(tmp_path: Path) -> None:
    async def _run() -> None:
        db = Database(tmp_path / "test.db")
        store = ArtifactStore(tmp_path)
        await db.connect()
        try:
            run_id = await db.create_run("http://localhost:3100", mode="single_day1")
            worker_id = await db.create_worker(run_id, "confused_first_timer", "Explore", "1280x800", 5)
            clean, som = store.step_screenshot_paths(run_id, worker_id, 1)
            clean.write_bytes(b"jpg")
            som.write_bytes(b"som")

            await db.insert_trace_step(
                run_id=run_id,
                worker_id=worker_id,
                step_index=1,
                action_type="click",
                locator={"primary_selector": "#hero-shop-btn"},
                input_value=None,
                thought="Open catalog",
                url="http://localhost:3100/catalog",
                screenshot_path=store.to_relative(clean),
                som_screenshot_path=store.to_relative(som),
                duration_ms=120,
            )
            await db.record_llm_usage(
                run_id=run_id,
                component="nav",
                model="gemini-2.5-flash",
                prompt_tokens=1000,
                output_tokens=100,
                cost_usd=0.00055,
            )
            steps = await db.list_trace_steps(run_id, worker_id)
            run = await db.get_run(run_id)
            assert len(steps) == 1
            assert steps[0]["action_type"] == "click"
            assert run is not None
            assert run["total_cost_usd"] == pytest.approx(0.00055)
        finally:
            await db.close()

    asyncio.run(_run())


def test_ground_truth_manifest_has_18_bugs() -> None:
    gt = json.loads((REPO_ROOT / "test-app" / "ground_truth.json").read_text(encoding="utf-8"))
    assert gt["total_seeded_bugs"] == 18
    assert len(gt["bugs"]) == 18
    categories = {b["category"] for b in gt["bugs"]}
    assert {
        "js_error",
        "api_failure",
        "validation_hole",
        "layout_break",
        "broken_flow",
        "dead_link",
        "xss",
    }.issubset(categories)


def test_gemini_planner_requires_key_and_schema_valid(tmp_path: Path) -> None:
    settings = Settings(
        gemini_api_key="",
        gemini_nav_model="gemini-2.5-flash",
        gemini_judge_model="gemini-2.5-pro",
        gemini_report_model="gemini-2.5-pro",
        gemini_embed_model="gemini-embedding-001",
        github_token="",
        github_default_repo="",
        headless=True,
        max_steps=10,
        max_wall_seconds=120,
        data_dir=tmp_path,
    )
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiActionPlanner(settings, Database(tmp_path / "x.db"))

    decision = ActionDecision(thought="Click button", action="click", element_id=1)
    assert decision.action == "click"


def test_gemini_rate_limit_retry_helper() -> None:
    delay = parse_retry_delay_seconds("429 RESOURCE_EXHAUSTED. Please retry in 9.72s.", attempt=0)
    assert 10.0 <= delay <= 12.0

    attempts = {"n": 0}

    async def _run() -> str:
        async def _flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED retry in 0.01s")
            return "ok"

        return await call_gemini_with_retry(_flaky, max_retries=2, label="test")

    assert asyncio.run(_run()) == "ok"
    assert attempts["n"] == 2


def test_novacart_and_playwright_worker_e2e(tmp_path: Path) -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async def _run() -> None:
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=str(REPO_ROOT / "test-app"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            ready = False
            for _ in range(40):
                try:
                    r = httpx.get(f"{base_url}/api/bootstrap", timeout=1.0)
                    if r.status_code == 200:
                        ready = True
                        break
                except Exception:
                    time.sleep(0.2)
            assert ready, "NovaCart server did not start in time"

            assert httpx.post(f"{base_url}/api/coupon", json={"code": "SAVE50"}).status_code == 500
            assert httpx.get(f"{base_url}/legal/terms-v2").status_code == 404
            assert httpx.post(f"{base_url}/__qa_reset").status_code == 200

            db = Database(tmp_path / "e2e.db")
            store = ArtifactStore(tmp_path)
            await db.connect()
            try:
                run_id = await db.create_run(base_url, mode="single_day1")
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    try:
                        result = await run_single_worker(
                            browser=browser,
                            db=db,
                            artifacts=store,
                            planner=HeuristicDryRunPlanner(),
                            run_id=run_id,
                            target_url=base_url,
                            persona=get_persona("hostile_fuzzer"),
                            max_steps=6,
                        )
                    finally:
                        await browser.close()

                steps = await db.list_trace_steps(run_id, result.worker_id)
                assert result.steps_executed == 6
                assert len(steps) == 6
                for st in steps:
                    assert store.resolve(st["screenshot_path"]).stat().st_size > 1000
                    assert store.resolve(st["som_screenshot_path"]).stat().st_size > 1000
            finally:
                await db.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    asyncio.run(_run())
