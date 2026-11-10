from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page

from detector.signatures import make_signature, normalize_url_path
from workers.browser_session import StepEvents

HEALTH_PROBE_JS = """
() => {
  const main = document.querySelector('main') || document.body;
  const bodyHtml = main ? main.innerHTML : '';
  const bodyText = document.body ? document.body.innerText.trim() : '';
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const docOverflow = document.documentElement.scrollWidth > vw + 2;

  const brokenImages = Array.from(document.querySelectorAll('img'))
    .filter(img => img.complete && img.naturalWidth === 0 && !img.getAttribute('onerror'))
    .map(img => img.getAttribute('src') || 'img');

  const textOverflows = [];
  for (const el of document.querySelectorAll('h1, h2, h3, .product-title, .card p, .card span')) {
    const parent = el.parentElement;
    if (!parent) continue;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    if (el.scrollWidth > parent.clientWidth + 8 && el.getBoundingClientRect().width > 0) {
      textOverflows.push((el.innerText || '').trim().slice(0, 60));
    }
  }

  const occludedControls = [];
  for (const btn of document.querySelectorAll('button, a.btn, input[type="submit"]')) {
    const style = window.getComputedStyle(btn);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const r = btn.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) continue;
    if (r.top >= 0 && r.bottom <= vh && r.left >= 0 && r.right <= vw) {
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (hit && !btn.contains(hit) && !hit.contains(btn)) {
        occludedControls.push((btn.innerText || btn.id || 'button').trim().slice(0, 50));
      }
    }
  }

  return {
    url: window.location.href,
    body_text_len: bodyText.length,
    main_html_len: bodyHtml.trim().length,
    dom_len: bodyHtml.length,
    dom_sample: bodyHtml.slice(0, 4000),
    doc_overflow: docOverflow,
    scroll_width: document.documentElement.scrollWidth,
    viewport_width: vw,
    broken_images: brokenImages.slice(0, 5),
    text_overflows: textOverflows.slice(0, 5),
    occluded_controls: occludedControls.slice(0, 5),
  };
}
"""


@dataclass
class PageHealthSnapshot:
    url: str
    dom_hash: str
    body_text_len: int
    main_html_len: int
    doc_overflow: bool
    scroll_width: int
    viewport_width: int
    broken_images: list[str]
    text_overflows: list[str]
    occluded_controls: list[str]

    @property
    def has_layout_anomaly(self) -> bool:
        return bool(
            self.doc_overflow
            or self.broken_images
            or self.text_overflows
            or self.occluded_controls
        )


@dataclass
class DetectedSuspect:
    category: str
    error_signature: str
    title_hint: str
    url: str
    evidence: dict[str, Any] = field(default_factory=dict)
    console_logs: list[dict[str, Any]] = field(default_factory=list)
    network_logs: list[dict[str, Any]] = field(default_factory=list)
    is_visual: bool = False


async def probe_page_health(page: Page) -> PageHealthSnapshot:
    try:
        raw: dict[str, Any] = await page.evaluate(HEALTH_PROBE_JS)
    except Exception:
        return PageHealthSnapshot(
            url=page.url,
            dom_hash="err",
            body_text_len=0,
            main_html_len=0,
            doc_overflow=False,
            scroll_width=0,
            viewport_width=0,
            broken_images=[],
            text_overflows=[],
            occluded_controls=[],
        )
    digest = hashlib.sha256(
        f"{raw.get('dom_len')}:{raw.get('dom_sample')}".encode("utf-8", errors="ignore")
    ).hexdigest()[:16]
    return PageHealthSnapshot(
        url=str(raw.get("url") or page.url),
        dom_hash=digest,
        body_text_len=int(raw.get("body_text_len") or 0),
        main_html_len=int(raw.get("main_html_len") or 0),
        doc_overflow=bool(raw.get("doc_overflow")),
        scroll_width=int(raw.get("scroll_width") or 0),
        viewport_width=int(raw.get("viewport_width") or 0),
        broken_images=list(raw.get("broken_images") or []),
        text_overflows=list(raw.get("text_overflows") or []),
        occluded_controls=list(raw.get("occluded_controls") or []),
    )


def evaluate_objective_signals(
    *,
    before: PageHealthSnapshot,
    after: PageHealthSnapshot,
    events: StepEvents,
    xss_hit: bool,
    action_type: str,
    locator_json: dict[str, Any],
    action_error: str | None,
) -> list[DetectedSuspect]:
    """Convert raw browser/DOM telemetry into structured suspect detections."""
    suspects: list[DetectedSuspect] = []
    url = after.url

    if xss_hit:
        suspects.append(
            DetectedSuspect(
                category="xss",
                error_signature=make_signature("xss", url, "script_or_handler_executed"),
                title_hint=f"XSS execution triggered on {normalize_url_path(url)}",
                url=url,
                evidence={"xss_triggered": True},
            )
        )

    for err in events.page_errors:
        suspects.append(
            DetectedSuspect(
                category="js_error",
                error_signature=make_signature("js_error", url, err),
                title_hint=f"Uncaught JS error on {normalize_url_path(url)}: {err[:80]}",
                url=url,
                evidence={"page_error": err},
                console_logs=[{"type": "pageerror", "text": err, "url": url}],
            )
        )

    for c_err in events.console_errors:
        text = str(c_err.get("text") or "")
        suspects.append(
            DetectedSuspect(
                category="js_error",
                error_signature=make_signature("console_error", url, text),
                title_hint=f"Console error on {normalize_url_path(url)}: {text[:80]}",
                url=url,
                evidence={"console_error": text},
                console_logs=[c_err],
            )
        )

    for net in events.network_failures:
        status = int(net.get("status") or 0)
        method = str(net.get("method") or "GET")
        req_url = str(net.get("url") or url)
        category = "dead_link" if (status == 404 and "/api/" not in req_url) else "api_failure"
        suspects.append(
            DetectedSuspect(
                category=category,
                error_signature=make_signature(category, req_url, f"{status}:{method}"),
                title_hint=f"HTTP {status} {method} on {normalize_url_path(req_url)}",
                url=url,
                evidence={"status": status, "method": method, "request_url": req_url},
                network_logs=[net],
            )
        )

    if events.crashed or (after.main_html_len == 0 and action_type in {"click", "dblclick", "navigate"}):
        suspects.append(
            DetectedSuspect(
                category="broken_flow",
                error_signature=make_signature("blank_screen", url, action_type),
                title_hint=f"Blank/crashed screen after {action_type} on {normalize_url_path(url)}",
                url=url,
                evidence={"crashed": events.crashed, "main_html_len": after.main_html_len},
            )
        )

    # Dead-end detection: interactive click/submit produced zero DOM, URL, or network response
    target_role = str(locator_json.get("role") or "")
    target_href = str(locator_json.get("href") or "")
    is_self_nav = (
        target_role == "link"
        and target_href.startswith("/")
        and normalize_url_path(url) == normalize_url_path(f"http://localhost{target_href}")
    )
    if (
        action_type == "click"
        and target_role in {"button", "link"}
        and not is_self_nav
        and not action_error
        and before.url == after.url
        and before.dom_hash == after.dom_hash
        and not events.network_failures
        and not events.page_errors
        and not events.console_errors
        and not xss_hit
    ):
        label = str(locator_json.get("role_name") or locator_json.get("primary_selector") or "control")
        suspects.append(
            DetectedSuspect(
                category="dead_link",
                error_signature=make_signature("dead_end", url, label),
                title_hint=f"Dead-end control '{label[:40]}' produced no state change on {normalize_url_path(url)}",
                url=url,
                evidence={"selector": locator_json.get("primary_selector"), "label": label},
            )
        )

    # Layout probes (objective geometry checks)
    if after.doc_overflow:
        suspects.append(
            DetectedSuspect(
                category="layout_break",
                error_signature=make_signature(
                    "layout_overflow", url, f"vw={after.viewport_width}"
                ),
                title_hint=f"Horizontal page overflow ({after.scroll_width}px > {after.viewport_width}px) on {normalize_url_path(url)}",
                url=url,
                evidence={
                    "scroll_width": after.scroll_width,
                    "viewport_width": after.viewport_width,
                    "type": "doc_overflow",
                },
                is_visual=True,
            )
        )

    if after.text_overflows:
        suspects.append(
            DetectedSuspect(
                category="layout_break",
                error_signature=make_signature(
                    "text_overflow", url, f"vw={after.viewport_width}"
                ),
                title_hint=f"Text/container overflow ({after.text_overflows[0][:40]}) on {normalize_url_path(url)}",
                url=url,
                evidence={"text_overflows": after.text_overflows, "type": "text_overflow"},
                is_visual=True,
            )
        )

    if after.occluded_controls:
        suspects.append(
            DetectedSuspect(
                category="layout_break",
                error_signature=make_signature(
                    "occluded_control", url, f"vw={after.viewport_width}:{after.occluded_controls[0]}"
                ),
                title_hint=f"Button '{after.occluded_controls[0]}' occluded by overlapping element on {normalize_url_path(url)}",
                url=url,
                evidence={"occluded_controls": after.occluded_controls, "type": "occluded_control"},
                is_visual=True,
            )
        )

    if after.broken_images:
        suspects.append(
            DetectedSuspect(
                category="layout_break",
                error_signature=make_signature("broken_image", url, after.broken_images[0]),
                title_hint=f"Broken image ({after.broken_images[0]}) on {normalize_url_path(url)}",
                url=url,
                evidence={"broken_images": after.broken_images, "type": "broken_image"},
                is_visual=True,
            )
        )

    return suspects

