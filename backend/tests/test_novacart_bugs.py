from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_all_18_novacart_seeded_bugs_trigger() -> None:
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

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    # Desktop context
                    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
                    page = await ctx.new_page()
                    page_errors: list[str] = []
                    console_errors: list[str] = []
                    bad_statuses: list[tuple[int, str]] = []
                    page.on("pageerror", lambda e: page_errors.append(str(e)))
                    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
                    page.on("response", lambda r: bad_statuses.append((r.status, r.url)) if r.status >= 400 else None)

                    # BUG-01: Quick View on product 101 throws TypeError
                    await page.goto(f"{base_url}/catalog", wait_until="networkidle")
                    await page.locator('.quick-view-btn[data-id="101"]').click()
                    assert any("specs" in e for e in page_errors), f"BUG-01 missed: {page_errors}"

                    # BUG-03: Sort by rating throws ReferenceError
                    await page.locator("#sort-select").select_option("rating")
                    assert any("sortByRating" in e for e in page_errors), f"BUG-03 missed: {page_errors}"

                    # BUG-17: Catalog search reflected XSS
                    await page.locator("#catalog-search").fill('<img src=x onerror="window.__qa_xss=true">')
                    await page.locator("#search-btn").click()
                    await page.wait_for_function("() => window.__qa_xss === true", timeout=3000)
                    await page.evaluate("() => { window.__qa_xss = false; }")

                    # BUG-02: Cart qty=0 throws RangeError
                    await page.goto(f"{base_url}/cart", wait_until="networkidle")
                    await page.locator(".qty-input").first.fill("0")
                    await page.locator(".qty-input").first.dispatch_event("change")
                    assert any("positive" in e for e in page_errors), f"BUG-02 missed: {page_errors}"

                    # BUG-08: Negative qty accepted -> negative total + console error
                    await page.locator(".qty-input").first.fill("-5")
                    await page.locator(".qty-input").first.dispatch_event("change")
                    await asyncio.sleep(0.3)
                    assert "$-" in await page.locator("#cart-total-display").inner_text(), "BUG-08 total missed"
                    assert any("Negative total" in c for c in console_errors), "BUG-08 console missed"

                    # BUG-04: SAVE50 promo -> HTTP 500
                    await page.locator("#coupon-input").fill("SAVE50")
                    await page.locator("#apply-coupon-btn").click()
                    await asyncio.sleep(0.3)
                    assert any(s == 500 and "/api/coupon" in u for s, u in bad_statuses), "BUG-04 500 missed"

                    # BUG-09: Checkout invalid ZIP -> HTTP 400 silently
                    await page.goto(f"{base_url}/checkout", wait_until="networkidle")
                    await page.locator("#chk-name").fill("Alex")
                    await page.locator("#chk-address").fill("123 Main")
                    await page.locator("#chk-zip").fill("abc")
                    await page.locator("#chk-exp").fill("01/19")
                    await page.locator("#checkout-next-btn").click()
                    await asyncio.sleep(0.3)
                    assert any(s == 400 and "/api/checkout/validate" in u for s, u in bad_statuses), "BUG-09 missed"

                    # BUG-14: Direct /checkout?step=2 -> TypeError
                    page_errors.clear()
                    await page.goto(f"{base_url}/checkout?step=2", wait_until="networkidle")
                    assert len(page_errors) >= 1, "BUG-14 step=2 crash missed"

                    # BUG-13: Double-clicking Place Order -> 500 & blank main
                    await page.goto(f"{base_url}/checkout", wait_until="networkidle")
                    await page.locator("#chk-name").fill("Alex")
                    await page.locator("#chk-address").fill("123 Main")
                    await page.locator("#chk-zip").fill("94107")
                    await page.locator("#chk-exp").fill("12/28")
                    await page.locator("#checkout-next-btn").click()
                    await page.locator("#place-order-btn").dblclick()
                    await asyncio.sleep(0.4)
                    assert any(s == 500 and "/api/orders" in u for s, u in bad_statuses), "BUG-13 500 missed"
                    assert (await page.locator("#app-main").inner_html()).strip() == "", "BUG-13 blank screen missed"

                    # BUG-06 & BUG-16: Orders invoice 404 & Track Package dead end
                    await page.goto(f"{base_url}/orders", wait_until="networkidle")
                    before_html = await page.locator("#app-main").inner_html()
                    await page.locator(".track-pkg-btn").first.click()
                    assert await page.locator("#app-main").inner_html() == before_html, "BUG-16 should have zero change"
                    await page.locator(".download-inv-btn").first.click()
                    await asyncio.sleep(0.3)
                    assert any(s == 404 and "/api/orders/invoice/undefined" in u for s, u in bad_statuses), "BUG-06 missed"

                    # BUG-05 & BUG-07: Profile bio > 200 -> 500, whitespace newsletter -> 422
                    await page.goto(f"{base_url}/profile", wait_until="networkidle")
                    await page.locator("#prof-bio").fill("A" * 250)
                    await page.locator("#save-profile-btn").click()
                    await asyncio.sleep(0.3)
                    assert any(s == 500 and "/api/profile" in u for s, u in bad_statuses), "BUG-05 missed"
                    await page.locator("#newsletter-email").fill("   ")
                    await page.locator("#newsletter-btn").click()
                    await asyncio.sleep(0.3)
                    assert any(s == 422 and "/api/newsletter" in u for s, u in bad_statuses), "BUG-07 missed"

                    # BUG-18: Support ticket XSS
                    await page.goto(f"{base_url}/support", wait_until="networkidle")
                    await page.locator("#tck-subject").fill('<img src=x onerror="window.__qa_xss=true">')
                    await page.locator("#submit-ticket-btn").click()
                    await page.wait_for_function("() => window.__qa_xss === true", timeout=3000)

                    # BUG-15: Footer Terms link 404
                    await page.goto(f"{base_url}/", wait_until="networkidle")
                    await page.locator('a[href="/legal/terms-v2"]').click()
                    assert any(s == 404 and "/legal/terms-v2" in u for s, u in bad_statuses), "BUG-15 missed"
                    await ctx.close()

                    # Mobile context for BUG-10, BUG-11, BUG-12
                    mctx = await browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
                    mpage = await mctx.new_page()
                    await mpage.goto(f"{base_url}/", wait_until="networkidle")
                    overflow = await mpage.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
                    assert overflow, "BUG-10 mobile navbar overflow missed"

                    await mpage.goto(f"{base_url}/catalog", wait_until="networkidle")
                    title_overflow = await mpage.evaluate(
                        """() => {
                            const titles = Array.from(document.querySelectorAll('.product-title'));
                            return titles.some(t => t.scrollWidth > t.parentElement.clientWidth);
                        }"""
                    )
                    assert title_overflow, "BUG-12 long title overflow missed"

                    await mpage.goto(f"{base_url}/cart", wait_until="networkidle")
                    occluded = await mpage.evaluate(
                        """() => {
                            const btn = document.getElementById('proceed-checkout-btn');
                            btn.scrollIntoView({block: 'end'});
                            const r = btn.getBoundingClientRect();
                            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                            return hit && !btn.contains(hit) && hit.closest('#mobile-promo-banner') !== null;
                        }"""
                    )
                    assert occluded, "BUG-11 mobile sticky banner occlusion missed"
                    await mctx.close()
                finally:
                    await browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    asyncio.run(_run())
