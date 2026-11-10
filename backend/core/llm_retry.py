from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

_RETRY_IN_RE = re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?([0-9]+)s", re.IGNORECASE)
_COOLDOWN_UNTIL: float = 0.0
_COOLDOWN_LOCK: asyncio.Lock | None = None


def _get_cooldown_lock() -> asyncio.Lock:
    global _COOLDOWN_LOCK
    if _COOLDOWN_LOCK is None:
        _COOLDOWN_LOCK = asyncio.Lock()
    return _COOLDOWN_LOCK


def parse_retry_delay_seconds(err_text: str, attempt: int) -> float:
    """Extract server-suggested retry delay from a Gemini 429 error, with bounded fallback."""
    for pat in (_RETRY_IN_RE, _RETRY_DELAY_RE):
        match = pat.search(err_text)
        if match:
            return min(65.0, max(1.0, float(match.group(1)) + 1.0))
    return min(60.0, 8.0 * (2**attempt))


def is_retryable_gemini_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in {429, 500, 503}:
        return True
    msg = str(exc)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg


async def call_gemini_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 4,
    label: str = "gemini",
) -> T:
    """Execute an async Gemini API call with automatic 429/503 rate-limit backoff and cooldown coordination."""
    global _COOLDOWN_UNTIL
    loop = asyncio.get_running_loop()

    for attempt in range(max_retries + 1):
        now = loop.time()
        if _COOLDOWN_UNTIL > now:
            await asyncio.sleep(_COOLDOWN_UNTIL - now)
        try:
            return await fn()
        except Exception as exc:
            if attempt >= max_retries or not is_retryable_gemini_error(exc):
                raise
            delay = parse_retry_delay_seconds(str(exc), attempt)
            async with _get_cooldown_lock():
                target_t = loop.time() + delay
                if target_t > _COOLDOWN_UNTIL:
                    _COOLDOWN_UNTIL = target_t
            print(f"[{label}] Rate-limited/transient error; retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(delay)
    raise RuntimeError("Unreachable retry loop exit")

