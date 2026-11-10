from __future__ import annotations

import re
from urllib.parse import urlparse

_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_NUMERIC_SEG_RE = re.compile(r"/\d+(?=/|$)")
_NUMBER_RE = re.compile(r"\b\d{3,}\b")


def normalize_url_path(url: str) -> str:
    """Extract normalized path (+ step query if present) without host/ID noise."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    path = _UUID_RE.sub("/{uuid}", path)
    path = _NUMERIC_SEG_RE.sub("/{id}", path)
    if parsed.query and "step=" in parsed.query:
        return f"{path}?{parsed.query}"
    return path


def normalize_message(text: str) -> str:
    cleaned = _UUID_RE.sub("{uuid}", text.strip().lower())
    cleaned = _NUMBER_RE.sub("{n}", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120]


def make_signature(category: str, url: str, key_part: str) -> str:
    path = normalize_url_path(url)
    norm_key = normalize_message(key_part)
    return f"{category}:{path}:{norm_key}"

