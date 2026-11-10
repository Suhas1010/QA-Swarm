from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from playwright.async_api import async_playwright

from detector.accessibility import audit_wcag_accessibility
from detector.objective import PageHealthSnapshot, evaluate_objective_signals
from reporter.github_client import _normalize_repo_slug
from reporter.report_writer import SEVERITY_RANK, _default_severity
from workers.browser_session import StepEvents


def test_wcag_accessibility_detector_catches_missing_alt_and_label() -> None:
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async def _run() -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <html><body>
                      <img src="x.png" width="40" height="40" />
                      <input type="text" placeholder="No label here" />
                      <button aria-label=""></button>
                    </body></html>
                    """
                )
                issues = await audit_wcag_accessibility(page)
                wcags = {i.evidence.get("wcag_criterion") for i in issues}
                assert "WCAG 1.1.1" in wcags
                assert "WCAG 1.3.1" in wcags
                assert "WCAG 4.1.2" in wcags
            finally:
                await browser.close()

    asyncio.run(_run())


def test_severity_ranking_and_accessibility_personas_endpoint() -> None:
    assert _normalize_repo_slug("https://github.com/Suhas1010/QA-Swarm.git") == "Suhas1010/QA-Swarm"
    assert _normalize_repo_slug("Suhas1010/QA-Swarm/") == "Suhas1010/QA-Swarm"
    assert SEVERITY_RANK[_default_severity("xss")] > SEVERITY_RANK[_default_severity("api_failure")]
    assert SEVERITY_RANK[_default_severity("api_failure")] > SEVERITY_RANK[_default_severity("accessibility")]

    import main as api_main

    with TestClient(api_main.app) as client:
        res = client.get("/api/personas")
        assert res.status_code == 200
        names = {p["name"] for p in res.json()}
        assert "accessibility_keyboard" in names
        assert "accessibility_low_vision" in names


def test_detector_filters_browser_and_third_party_noise_keeps_genuine() -> None:
    snap = PageHealthSnapshot(
        url="https://www.food.com/",
        dom_hash="abc",
        body_text_len=500,
        main_html_len=1200,
        doc_overflow=False,
        scroll_width=1280,
        viewport_width=1280,
        broken_images=[],
        text_overflows=[],
        occluded_controls=[],
    )
    ev = StepEvents(
        console_errors=[
            {"type": "error", "text": "Permissions policy violation: unload is not allowed in this document."},
            {"type": "error", "text": "Failed to load resource: the server responded with a status of 401 ()"},
            {"type": "error", "text": "Access to XMLHttpRequest at 'https://ep2.adtrafficquality.google/sodar' blocked by CORS"},
            {"type": "error", "text": "Uncaught application state failure on checkout"},
        ],
        network_failures=[
            {"status": 401, "method": "POST", "url": "https://www.food.com/sso/api/1/check"},
            {"status": 404, "method": "GET", "url": "https://www.food.com/ideas/5-ingredient/__data.json"},
            {"status": 500, "method": "POST", "url": "https://www.food.com/api/checkout"},
        ],
    )
    suspects = evaluate_objective_signals(
        before=snap,
        after=snap,
        events=ev,
        xss_hit=False,
        action_type="click",
        locator_json={"role": "button", "is_disabled": True},
        action_error=None,
    )
    titles = [s.title_hint for s in suspects]
    assert len(suspects) == 2
    assert any("Uncaught application state failure" in t for t in titles)
    assert any("HTTP 500 POST on /api/checkout" in t for t in titles)

