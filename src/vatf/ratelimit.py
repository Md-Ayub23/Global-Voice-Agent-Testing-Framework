"""Rate limits and the queue in front of the AI model, shared by everything in one Vocto process.

Two jobs:

1. **The AI queue (LLMGate).** Every Claude request (callers, judges, plan writer, analysis, real-call
   judging) waits for a slot here first:
     - at most `VOCTO_LLM_CONCURRENCY` requests at once (default 4) in this process,
     - at most `VOCTO_LLM_RPM` requests a minute (default 50, a token bucket, so short bursts are fine),
     - and none while the provider has told any Vocto process to back off (a 429 / 529 "retry after" is
       written to a small shared file, so a dashboard and the runs it started all slow down together).
   A request that fails for a reason worth retrying (429 rate limit, 529 overloaded, 5xx, a dropped
   connection or a timeout) is retried with exponential backoff and jitter, honouring the provider's
   retry-after, up to `VOCTO_LLM_RETRIES` times (default 5). Anything else (a bad request, a wrong key)
   fails at once.

2. **API rate limits (RateLimiter).** A token bucket per client (user or address) for the dashboard's
   API, the login form and the real-call webhook, so one noisy client can't starve the others.

Numbers come from the environment so a server can be tuned without a code change.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

T = TypeVar("T")


def _env_int(name: str, default: int, low: int = 1, high: int = 10_000) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, default))))
    except ValueError:
        return default


def _env_float(name: str, default: float, low: float = 0.0, high: float = 1e9) -> float:
    try:
        return max(low, min(high, float(os.environ.get(name, default))))
    except ValueError:
        return default


class TokenBucket:
    """`rate` tokens a second, holding at most `burst`. Thread-safe."""

    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = float(rate), float(burst)
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def take(self, n: float = 1.0) -> float:
        """Takes n tokens if there are enough and returns 0; otherwise takes nothing and returns how many
        seconds until there will be."""
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self.tokens >= n:
                self.tokens -= n
                return 0.0
            return (n - self.tokens) / self.rate if self.rate > 0 else 60.0


class RateLimiter:
    """A token bucket per key (a user, an address). Old idle keys are forgotten."""

    def __init__(self, per_minute: float, burst: float | None = None, max_keys: int = 50_000):
        self.rate = per_minute / 60.0
        self.burst = burst if burst is not None else max(1.0, per_minute / 6)
        self.max_keys = max_keys
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str, cost: float = 1.0) -> float:
        """0 when the request may go ahead, else the seconds to wait (the Retry-After)."""
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    now = time.monotonic()
                    idle = [k for k, b in self._buckets.items() if now - b.updated > 600]
                    for k in idle or list(self._buckets)[: self.max_keys // 10]:
                        del self._buckets[k]
                bucket = self._buckets[key] = TokenBucket(self.rate, self.burst)
        return bucket.take(cost)

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)


# ---- the AI queue ------------------------------------------------------------------------------------

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def _cooldown_file() -> Path:
    return Path(os.environ.get("VOCTO_LLM_COOLDOWN_FILE") or Path(tempfile.gettempdir()) / "vocto-llm-cooldown.json")


def retry_after_s(error: BaseException) -> float | None:
    """The provider's own "retry after" (seconds), when the error carries one."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or {}
    for name in ("retry-after-ms", "retry-after"):
        raw = headers.get(name) if hasattr(headers, "get") else None
        if raw:
            try:
                value = float(raw)
            except ValueError:
                continue
            return value / 1000.0 if name.endswith("-ms") else value
    return None


def is_retryable(error: BaseException) -> bool:
    """Worth another try: rate limited, overloaded, a server error, a timeout or a dropped connection."""
    name = type(error).__name__
    if name in ("APIConnectionError", "APITimeoutError", "RateLimitError", "OverloadedError",
                "InternalServerError", "ServiceUnavailableError", "RetryableError", "DeadlineExceededError"):
        return True
    status = getattr(error, "status_code", None)
    return isinstance(status, int) and status in RETRY_STATUS


class LLMGate:
    """The queue every AI request goes through (see the module notes)."""

    def __init__(self, concurrency: int | None = None, rpm: float | None = None, retries: int | None = None,
                 base_delay_s: float | None = None, max_delay_s: float | None = None):
        self.concurrency = concurrency or _env_int("VOCTO_LLM_CONCURRENCY", 4, 1, 256)
        self.rpm = rpm or _env_float("VOCTO_LLM_RPM", 50, 1, 100_000)
        self.retries = retries if retries is not None else _env_int("VOCTO_LLM_RETRIES", 5, 0, 20)
        self.base_delay_s = base_delay_s if base_delay_s is not None else _env_float("VOCTO_LLM_BACKOFF_S", 1.0, 0, 60)
        self.max_delay_s = max_delay_s if max_delay_s is not None else _env_float("VOCTO_LLM_BACKOFF_MAX_S", 60.0, 0, 600)
        self._slots = threading.BoundedSemaphore(self.concurrency)
        self._bucket = TokenBucket(self.rpm / 60.0, max(1.0, min(self.rpm, self.concurrency * 2)))
        self.waiting = 0
        self.running = 0
        self.retried = 0
        self.rate_limited = 0
        self._lock = threading.Lock()

    # Shared back-off (between processes): "nobody call before <until>".
    def cooldown_left(self) -> float:
        try:
            until = float(json.loads(_cooldown_file().read_text(encoding="utf-8")).get("until", 0))
        except (OSError, ValueError, AttributeError):
            return 0.0
        return max(0.0, until - time.time())

    def cool_down(self, seconds: float) -> None:
        seconds = min(max(seconds, 0.0), self.max_delay_s or 60.0)
        if seconds <= 0 or self.cooldown_left() >= seconds:
            return
        try:
            from .util import write_text_atomic

            path = _cooldown_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, json.dumps({"until": time.time() + seconds}))
        except OSError:
            pass

    def backoff_s(self, attempt: int, error: BaseException | None = None) -> float:
        told = retry_after_s(error) if error is not None else None
        if told is not None:
            return min(told, self.max_delay_s) + random.uniform(0, 0.25)
        # Full jitter: anywhere from 0 to base * 2^attempt, so many waiting clients don't retry in step.
        return random.uniform(0, min(self.max_delay_s, self.base_delay_s * (2 ** attempt)))

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        with self._lock:
            self.waiting += 1
        acquired = False
        try:
            # A thread semaphore (not an asyncio one): the dashboard runs requests on many event loops.
            while not self._slots.acquire(blocking=False):
                await asyncio.sleep(0.05)
            acquired = True
            while True:
                wait = max(self.cooldown_left(), self._bucket.take())
                if wait <= 0:
                    break
                await asyncio.sleep(min(wait, 5.0))
        except BaseException:
            if acquired:
                self._slots.release()
            raise
        finally:
            with self._lock:
                self.waiting -= 1
        with self._lock:
            self.running += 1
        try:
            yield
        finally:
            with self._lock:
                self.running -= 1
            self._slots.release()

    async def call(self, send: Callable[[], Awaitable[T]], on_retry: Callable[[int, BaseException, float], None] | None = None) -> T:
        """send() through the queue, retried as the module notes say. The last error is raised as it is."""
        attempt = 0
        while True:
            try:
                async with self.slot():
                    return await send()
            except Exception as e:  # noqa: BLE001 - classified below; the rest are raised unchanged
                if not is_retryable(e) or attempt >= self.retries:
                    raise
                delay = self.backoff_s(attempt, e)
                status = getattr(e, "status_code", None)
                with self._lock:
                    self.retried += 1
                    if status in (429, 529) or type(e).__name__ in ("RateLimitError", "OverloadedError"):
                        self.rate_limited += 1
                if status in (429, 529) or type(e).__name__ in ("RateLimitError", "OverloadedError"):
                    self.cool_down(delay)       # every Vocto process waits, not only this request
                if on_retry:
                    on_retry(attempt + 1, e, delay)
                attempt += 1
                await asyncio.sleep(delay)

    def stats(self) -> dict[str, Any]:
        return {"concurrency": self.concurrency, "per_minute": self.rpm, "retries": self.retries,
                "running": self.running, "waiting": self.waiting, "retried": self.retried,
                "rate_limited": self.rate_limited, "cooling_down_s": round(self.cooldown_left(), 1)}


_GATE: LLMGate | None = None
_GATE_LOCK = threading.Lock()


def llm_gate() -> LLMGate:
    """This process's AI queue."""
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            _GATE = LLMGate()
        return _GATE


def reset_llm_gate(gate: LLMGate | None = None) -> None:
    """Tests (and a settings change) swap the queue."""
    global _GATE
    with _GATE_LOCK:
        _GATE = gate
