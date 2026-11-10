from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from playwright.async_api import async_playwright

from detector.accessibility import audit_wcag_accessibility
from reporter.github_client import _normalize_repo_slug
from reporter.report_writer import SEVERITY_RANK, _default_severity


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

