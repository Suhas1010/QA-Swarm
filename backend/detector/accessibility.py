from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from detector.objective import DetectedSuspect
from detector.signatures import make_signature, normalize_url_path

WCAG_AUDIT_JS = """
() => {
  const issues = [];
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  // WCAG 1.1.1 Non-text Content: visible <img> missing alt
  for (const img of document.querySelectorAll('img')) {
    const r = img.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const alt = img.getAttribute('alt');
    if (alt === null || alt.trim() === '') {
      issues.push({
        wcag: 'WCAG 1.1.1',
        kind: 'missing_alt',
        detail: `Image missing alt attribute (${img.getAttribute('src') || 'img'})`,
      });
      break;
    }
  }

  // WCAG 4.1.2 / 1.3.1: visible inputs without associated label or aria-label
  for (const inp of document.querySelectorAll('input:not([type="hidden"]), select, textarea')) {
    const style = window.getComputedStyle(inp);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const id = inp.id;
    const hasForLabel = id ? Boolean(document.querySelector(`label[for="${CSS.escape(id)}"]`)) : false;
    const hasWrapLabel = Boolean(inp.closest('label'));
    const hasAria = Boolean(inp.getAttribute('aria-label') || inp.getAttribute('aria-labelledby'));
    if (!hasForLabel && !hasWrapLabel && !hasAria) {
      issues.push({
        wcag: 'WCAG 1.3.1',
        kind: 'unlabeled_control',
        detail: `Form control <${inp.tagName.toLowerCase()}> (${inp.id || inp.placeholder || 'unnamed'}) missing explicit <label> or aria-label`,
      });
      break;
    }
  }

  // WCAG 2.4.7 Focus Visible / 4.1.2 empty button name
  for (const btn of document.querySelectorAll('button, [role="button"], a[href]')) {
    const r = btn.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.top > vh || r.left > vw) continue;
    const name = (btn.innerText || btn.getAttribute('aria-label') || btn.getAttribute('title') || '').trim();
    if (!name) {
      issues.push({
        wcag: 'WCAG 4.1.2',
        kind: 'empty_accessible_name',
        detail: `Interactive <${btn.tagName.toLowerCase()}> has no accessible name`,
      });
      break;
    }
  }

  // WCAG 1.4.3 Contrast heuristic on visible text vs explicit background
  function parseRgb(str) {
    const m = (str || '').match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);
    if (!m) return null;
    if (m[4] !== undefined && Number(m[4]) === 0) return null;
    return [Number(m[1]), Number(m[2]), Number(m[3])];
  }
  function relLum(rgb) {
    const c = rgb.map(v => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  for (const el of document.querySelectorAll('p, span, a, button, label')) {
    const txt = (el.innerText || '').trim();
    if (!txt) continue;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const fg = parseRgb(st.color);
    const bg = parseRgb(st.backgroundColor);
    if (fg && bg) {
      const l1 = relLum(fg);
      const l2 = relLum(bg);
      const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      if (ratio < 3.0) {
        issues.push({
          wcag: 'WCAG 1.4.3',
          kind: 'low_contrast',
          detail: `Low contrast text (${ratio.toFixed(2)}:1) on "${txt.slice(0, 30)}"`,
        });
        break;
      }
    }
  }

  return issues;
}
"""


async def audit_wcag_accessibility(page: Page) -> list[DetectedSuspect]:
    """Run WCAG 2.1 AA accessibility checks for accessibility personas."""
    try:
        raw_issues: list[dict[str, Any]] = await page.evaluate(WCAG_AUDIT_JS)
    except Exception:
        return []

    url = page.url
    suspects: list[DetectedSuspect] = []
    for item in raw_issues:
        wcag = str(item.get("wcag") or "WCAG 2.1")
        kind = str(item.get("kind") or "a11y")
        detail = str(item.get("detail") or "Accessibility defect")
        suspects.append(
            DetectedSuspect(
                category="accessibility",
                error_signature=make_signature("accessibility", url, f"{wcag}:{kind}"),
                title_hint=f"[{wcag}] {detail} on {normalize_url_path(url)}",
                url=url,
                evidence={"wcag_criterion": wcag, "kind": kind, "detail": detail},
            )
        )
    return suspects

