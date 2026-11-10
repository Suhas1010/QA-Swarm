from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

_RETRY_IN_RE = re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?([0-9]+)s", re.IGNORECASE)

DEFAULT_NAV_FALLBACKS = ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash"]
DEFAULT_STRONG_FALLBACKS = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]

_COOLDOWN_UNTIL: float = 0.0
_COOLDOWN_LOCK: asyncio.Lock | None = None
_API_SEMAPHORE: asyncio.Semaphore | None = None
# Cache models that returned 404 or daily quota exhaustion so workers fail over in 0ms
_EXHAUSTED_MODELS: dict[str, float] = {}


def _get_cooldown_lock() -> asyncio.Lock:
    global _COOLDOWN_LOCK
    if _COOLDOWN_LOCK is None:
        _COOLDOWN_LOCK = asyncio.Lock()
    return _COOLDOWN_LOCK


def _get_api_semaphore() -> asyncio.Semaphore:
    global _API_SEMAPHORE
    if _API_SEMAPHORE is None:
        _API_SEMAPHORE = asyncio.Semaphore(2)
    return _API_SEMAPHORE


def parse_retry_delay_seconds(err_text: str, attempt: int) -> float:
    """Extract server-suggested retry delay from a Gemini 429 error, with bounded fallback."""
    for pat in (_RETRY_IN_RE, _RETRY_DELAY_RE):
        match = pat.search(err_text)
        if match:
            return min(35.0, max(1.0, float(match.group(1)) + 1.0))
    return min(30.0, 5.0 * (2**attempt))


def _raw_retry_seconds(err_text: str) -> float | None:
    for pat in (_RETRY_IN_RE, _RETRY_DELAY_RE):
        match = pat.search(err_text)
        if match:
            return float(match.group(1))
    return None


def is_retryable_gemini_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in {429, 500, 503}:
        return True
    msg = str(exc)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg or "UNAVAILABLE" in msg


def is_permanent_or_daily_exhausted(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    msg = str(exc)
    if code == 404 or "404 NOT_FOUND" in msg or "is no longer available" in msg:
        return True
    if "PerDay" in msg or "requests per day" in msg.lower():
        return True
    raw_delay = _raw_retry_seconds(msg)
    if raw_delay is not None and raw_delay > 90.0:
        return True
    return False


async def call_gemini_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 2,
    label: str = "gemini",
) -> T:
    """Execute an async Gemini API call with automatic short-backoff retry."""
    global _COOLDOWN_UNTIL
    loop = asyncio.get_running_loop()

    for attempt in range(max_retries + 1):
        now = loop.time()
        if _COOLDOWN_UNTIL > now:
            await asyncio.sleep(min(25.0, _COOLDOWN_UNTIL - now))
        try:
            async with _get_api_semaphore():
                return await fn()
        except Exception as exc:
            if is_permanent_or_daily_exhausted(exc):
                raise
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


async def call_gemini_with_fallback(
    primary_model: str,
    fallback_models: list[str],
    call_for_model: Callable[[str], Awaitable[T]],
    *,
    label: str = "gemini",
) -> tuple[T, str]:
    """Try primary_model then active fallback_models, skipping daily-exhausted or 404 models."""
    candidates: list[str] = []
    for m in [primary_model, *fallback_models]:
        if m and m not in candidates:
            candidates.append(m)

    now_wall = time.monotonic()
    last_exc: Exception | None = None
    for model in candidates:
        skip_until = _EXHAUSTED_MODELS.get(model, 0.0)
        if skip_until > now_wall:
            continue
        try:
            res = await call_gemini_with_retry(
                lambda m=model: call_for_model(m),
                max_retries=1,
                label=f"{label}:{model}",
            )
            return res, model
        except Exception as exc:
            last_exc = exc
            if is_permanent_or_daily_exhausted(exc) or is_retryable_gemini_error(exc):
                ttl = 300.0 if is_permanent_or_daily_exhausted(exc) else 20.0
                _EXHAUSTED_MODELS[model] = time.monotonic() + ttl
                print(f"[{label}] Failing over from {model} ({str(exc)[:100]})")
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("No Gemini models available in fallback chain")
