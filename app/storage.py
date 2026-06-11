"""Pluggable key-value storage for metering and caches.

The gateway needs a small set of storage operations: store/fetch a JSON blob
(key configs), atomically increment a set of counters with a TTL (usage), read
a counter hash, and scan keys by prefix (admin listing).

Two backends implement this:

- ``RedisStore``  — wraps a Redis client; the production / multi-process choice.
- ``MemoryStore`` — in-process, thread-safe, with lazy TTL expiry. This is the
  Zero-Config default: ``docker run`` works with no Redis, and per-key quotas
  still function. Trade-off: in-memory state resets on restart, which is fine
  for daily/monthly windows in a single-container deployment.

A persistent single-node backend (e.g. SQLite) or a coordinated multi-node
backend (Enterprise) can implement the same protocol without touching callers.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class KeyValueStore(Protocol):
    """Storage operations required by the metering service."""

    def get_str(self, key: str) -> str | None: ...

    def set_str(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def incr_fields(self, key: str, fields: dict[str, int], ttl_seconds: int) -> dict[str, int]:
        """Atomically add to each counter field, set/refresh TTL, return the
        full resulting field map (all integer counters for the key)."""
        ...

    def get_fields(self, key: str) -> dict[str, int]:
        """Return all integer counter fields for a key (empty dict if absent)."""
        ...

    def scan_prefix(self, prefix: str) -> list[str]:
        """Return all keys starting with ``prefix``."""
        ...


class RedisStore:
    """KeyValueStore backed by a Redis client (decode_responses=False)."""

    def __init__(self, redis_client: object) -> None:
        self._r = redis_client

    def get_str(self, key: str) -> str | None:
        raw = self._r.get(key)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    def set_str(self, key: str, value: str) -> None:
        self._r.set(key, value)

    def delete(self, key: str) -> None:
        self._r.delete(key)

    def incr_fields(self, key: str, fields: dict[str, int], ttl_seconds: int) -> dict[str, int]:
        pipe = self._r.pipeline()
        for field, amount in fields.items():
            pipe.hincrby(key, field, amount)
        pipe.expire(key, ttl_seconds)
        pipe.execute()
        return self.get_fields(key)

    def get_fields(self, key: str) -> dict[str, int]:
        raw = self._r.hgetall(key)
        if not raw:
            return {}
        out: dict[str, int] = {}
        for k, v in raw.items():
            kk = k.decode() if isinstance(k, bytes) else k
            try:
                out[kk] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    def scan_prefix(self, prefix: str) -> list[str]:
        keys: list[str] = []
        cursor = 0
        while True:
            cursor, found = self._r.scan(cursor, match=f"{prefix}*", count=100)
            for raw in found:
                keys.append(raw.decode() if isinstance(raw, bytes) else raw)
            if cursor == 0:
                break
        return keys


class MemoryStore:
    """In-process, thread-safe KeyValueStore with lazy TTL expiry.

    The Zero-Config default. State lives in the process and resets on restart.
    """

    def __init__(self, *, now: "callable | None" = None) -> None:
        self._now = now or time.time
        self._lock = threading.Lock()
        self._str: dict[str, str] = {}
        self._fields: dict[str, dict[str, int]] = {}
        self._expiry: dict[str, float] = {}

    def _expired(self, key: str) -> bool:
        exp = self._expiry.get(key)
        return exp is not None and self._now() >= exp

    def _evict_if_expired(self, key: str) -> None:
        if self._expired(key):
            self._str.pop(key, None)
            self._fields.pop(key, None)
            self._expiry.pop(key, None)

    def get_str(self, key: str) -> str | None:
        with self._lock:
            self._evict_if_expired(key)
            return self._str.get(key)

    def set_str(self, key: str, value: str) -> None:
        with self._lock:
            self._str[key] = value
            self._expiry.pop(key, None)  # string configs do not expire

    def delete(self, key: str) -> None:
        with self._lock:
            self._str.pop(key, None)
            self._fields.pop(key, None)
            self._expiry.pop(key, None)

    def incr_fields(self, key: str, fields: dict[str, int], ttl_seconds: int) -> dict[str, int]:
        with self._lock:
            self._evict_if_expired(key)
            bucket = self._fields.setdefault(key, {})
            for field, amount in fields.items():
                bucket[field] = bucket.get(field, 0) + amount
            self._expiry[key] = self._now() + ttl_seconds
            return dict(bucket)

    def get_fields(self, key: str) -> dict[str, int]:
        with self._lock:
            self._evict_if_expired(key)
            return dict(self._fields.get(key, {}))

    def scan_prefix(self, prefix: str) -> list[str]:
        with self._lock:
            for key in list(self._expiry):
                self._evict_if_expired(key)
            seen = set(self._str) | set(self._fields)
            return [k for k in seen if k.startswith(prefix)]
