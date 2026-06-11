"""Per-key request rate limiting and concurrency limiting.

Token quotas (see token_metering) cap total spend; they do not protect against
bursts or a single key hammering the gateway. This module adds two cheap,
in-memory guards per API key:

- a token bucket for requests-per-minute (with an optional burst allowance)
- a concurrency limiter for in-flight requests

In-memory keeps the single-container promise (no Redis required). A multi-node
deployment would coordinate these centrally — that is an Enterprise concern and
intentionally out of scope for the core.

All limits are opt-in: a value of 0 disables that limit.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """Token-bucket rate limiter + concurrency limiter, keyed by API key."""

    def __init__(
        self,
        per_minute: int = 0,
        burst: int = 0,
        concurrency: int = 0,
        *,
        now: "callable | None" = None,
    ) -> None:
        self._per_minute = max(0, per_minute)
        # Bucket capacity = burst if given, else the per-minute rate.
        self._capacity = float(burst if burst > 0 else per_minute)
        self._refill_per_sec = self._per_minute / 60.0 if self._per_minute else 0.0
        self._concurrency = max(0, concurrency)
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._inflight: dict[str, int] = {}

    @property
    def rate_enabled(self) -> bool:
        return self._per_minute > 0

    @property
    def concurrency_enabled(self) -> bool:
        return self._concurrency > 0

    def check_rate(self, key: str) -> bool:
        """Consume one request token for ``key``. Returns False if rate exceeded."""
        if not self.rate_enabled:
            return True
        now = self._now()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._buckets[key] = _Bucket(tokens=self._capacity - 1, last_refill=now)
                return True
            elapsed = now - bucket.last_refill
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_sec)
            bucket.last_refill = now
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return True
            return False

    def try_acquire(self, key: str) -> bool:
        """Reserve a concurrency slot for ``key``. Returns False if at capacity.

        On success the caller MUST call ``release(key)`` when the request ends.
        """
        if not self.concurrency_enabled:
            return True
        with self._lock:
            current = self._inflight.get(key, 0)
            if current >= self._concurrency:
                return False
            self._inflight[key] = current + 1
            return True

    def release(self, key: str) -> None:
        if not self.concurrency_enabled:
            return
        with self._lock:
            current = self._inflight.get(key, 0)
            if current <= 1:
                self._inflight.pop(key, None)
            else:
                self._inflight[key] = current - 1
