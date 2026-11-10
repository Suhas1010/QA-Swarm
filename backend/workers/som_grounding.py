from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import Page

DOM_EXTRACTION_JS = """
() => {
  const selectors = [
    'a[href]', 'button', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[onclick]', 'summary'
  ].join(',');

  const nodes = Array.from(document.querySelectorAll(selectors));
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const results = [];

  function cssPath(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName.toLowerCase() !== 'html') {
      let sel = cur.tagName.toLowerCase();
      if (cur.id) {
        parts.unshift('#' + CSS.escape(cur.id));
        break;
      }
      let idx = 1;
      let sib = cur.previousElementSibling;
      while (sib) {
        if (sib.tagName === cur.tagName) idx++;
        sib = sib.previousElementSibling;
      }
      sel += `:nth-of-type(${idx})`;
      parts.unshift(sel);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  function inferRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (['button', 'submit', 'reset'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      return 'textbox';
    }
    return '';
  }

  for (const el of nodes) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
      continue;
    }
    let rect = el.getBoundingClientRect();
    if ((rect.width < 6 || rect.height < 6) && el.firstElementChild) {
      rect = el.firstElementChild.getBoundingClientRect();
    }
    if ((rect.width < 6 || rect.height < 6) && el.getClientRects().length > 0) {
      rect = el.getClientRects()[0];
    }
    if (rect.width < 6 || rect.height < 6) continue;
    if (rect.bottom < 0 || rect.top > vh || rect.right < 0 || rect.left > vw) continue;

    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '')
      .replace(/\\s+/g, ' ')
      .trim()
      .slice(0, 80);

    const testId = el.getAttribute('data-testid');
    const id = el.id || null;
    const nameAttr = el.getAttribute('name');
    const ariaLabel = el.getAttribute('aria-label');
    const role = inferRole(el);

    let primarySelector = cssPath(el);
    if (testId) {
      primarySelector = `[data-testid="${CSS.escape(testId)}"]`;
    } else if (id) {
      primarySelector = `#${CSS.escape(id)}`;
    } else if (nameAttr) {
      primarySelector = `${tag}[name="${CSS.escape(nameAttr)}"]`;
    }

    const options = tag === 'select'
      ? Array.from(el.options || []).slice(0, 10).map(o => ({ value: o.value, label: o.text.trim() }))
      : [];

    results.push({
      id: results.length + 1,
      tag,
      type: el.getAttribute('type') || null,
      role,
      text,
      placeholder: el.getAttribute('placeholder') || null,
      value: tag === 'input' || tag === 'textarea' || tag === 'select' ? String(el.value || '').slice(0, 40) : null,
      href: tag === 'a' ? el.getAttribute('href') : null,
      options,
      locator: {
        primary_selector: primarySelector,
        css_path: cssPath(el),
        testid: testId,
        element_id: id,
        name: nameAttr,
        aria_label: ariaLabel,
        role,
        role_name: text || ariaLabel || null,
        href: tag === 'a' ? el.getAttribute('href') : null,
        target_blank: tag === 'a' ? el.getAttribute('target') === '_blank' : false,
        is_disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
        bbox: {
          x: Math.round(rect.left),
          y: Math.round(rect.top),
          w: Math.round(rect.width),
          h: Math.round(rect.height),
        }
      }
    });

    if (results.length >= 75) break;
  }

  return {
    title: document.title,
    url: window.location.href,
    viewport_width: vw,
    viewport_height: vh,
    scroll_width: document.documentElement.scrollWidth,
    scroll_height: document.documentElement.scrollHeight,
    body_text_len: (document.body ? document.body.innerText.trim().length : 0),
    elements: results
  };
}
"""


def _annotate_som_image(clean_jpeg: bytes, elements: list[dict[str, Any]]) -> bytes:
    """Draw numbered Set-of-Marks boxes onto a Pillow copy without modifying the DOM."""
    img = Image.open(io.BytesIO(clean_jpeg)).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    palette = ["#ef4444", "#2563eb", "#16a34a", "#d97706", "#9333ea"]
    for el in elements:
        eid = el["id"]
        bbox = el["locator"]["bbox"]
        x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
        color = palette[eid % len(palette)]
        draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
        tag = f"[{eid}]"
        tx = max(0, x)
        ty = max(0, y - 13)
        tw = len(tag) * 6 + 6
        draw.rectangle([tx, ty, tx + tw, ty + 13], fill=color)
        draw.text((tx + 3, ty + 1), tag, fill="#ffffff", font=font)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=75)
    return out.getvalue()


async def capture_som_perception(
    page: Page,
    clean_path: Path,
    som_path: Path,
) -> tuple[bytes, bytes, dict[str, Any]]:
    """Capture clean screenshot, extract interactive DOM manifest, and render SoM image."""
    clean_jpeg = await page.screenshot(type="jpeg", quality=75, full_page=False)
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    clean_path.write_bytes(clean_jpeg)

    dom_state: dict[str, Any] = await page.evaluate(DOM_EXTRACTION_JS)
    som_jpeg = _annotate_som_image(clean_jpeg, dom_state.get("elements", []))
    som_path.write_bytes(som_jpeg)
    return clean_jpeg, som_jpeg, dom_state

