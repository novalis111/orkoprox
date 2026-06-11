"""Semantic response cache (F3, optional, off by default).

Caches chat-completion responses keyed by an embedding of the request. When a
new request embeds close enough (cosine similarity >= threshold) to a cached
one, the cached response is returned instead of calling the provider — cutting
cost and latency for repeated or paraphrased prompts.

Design notes:
- Local and in-process (no extra dependency, no numpy — cosine is pure Python).
  Fits the single-container promise; cache resets on restart.
- LRU eviction beyond ``max_entries`` and optional per-entry TTL.
- The cache itself needs one embedding call per request, but embeddings are far
  cheaper than chat completions, so a hit is a large net saving. Off by default.
- Brute-force nearest-neighbour scan: fine for the small local caches this is
  meant for. A vector index is an Enterprise/scale concern, out of core scope.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors. 0.0 on degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


@dataclass
class _Entry:
    embedding: list[float]
    response: dict[str, Any]
    created: float


class SemanticCache:
    """In-process semantic cache with LRU eviction and optional TTL."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        threshold: float = 0.95,
        max_entries: int = 1000,
        ttl_seconds: int = 3600,
        now: "callable | None" = None,
    ) -> None:
        self._enabled = enabled
        self._threshold = threshold
        self._max_entries = max(1, max_entries)
        self._ttl = ttl_seconds
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _is_expired(self, entry: _Entry) -> bool:
        return self._ttl > 0 and (self._now() - entry.created) >= self._ttl

    def lookup(self, embedding: list[float]) -> dict[str, Any] | None:
        """Return a cached response whose key embeds within threshold, or None."""
        if not self._enabled or not embedding:
            return None
        best_key: str | None = None
        best_sim = self._threshold
        with self._lock:
            for key, entry in list(self._entries.items()):
                if self._is_expired(entry):
                    self._entries.pop(key, None)
                    continue
                sim = cosine_similarity(embedding, entry.embedding)
                if sim >= best_sim:
                    best_sim = sim
                    best_key = key
            if best_key is None:
                return None
            entry = self._entries.pop(best_key)
            self._entries[best_key] = entry  # mark as most-recently-used
            return entry.response

    def store(self, key: str, embedding: list[float], response: dict[str, Any]) -> None:
        """Cache a response under ``key`` with its request embedding."""
        if not self._enabled or not embedding:
            return
        with self._lock:
            self._entries[key] = _Entry(
                embedding=embedding, response=response, created=self._now()
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)
