"""Unit tests for the SemanticCache (F3) and cosine_similarity helper."""

from __future__ import annotations

import math

import pytest

from app.semantic_cache import SemanticCache, cosine_similarity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_clock(start: float = 1_000.0) -> tuple[list[float], "callable[[], float]"]:
    """Return a mutable clock state list and a callable that reads it."""
    t = [start]

    def now() -> float:
        return t[0]

    return t, now


def _unit(angle_rad: float) -> list[float]:
    """Unit vector at *angle_rad* in 2-D: [cos θ, sin θ]."""
    return [math.cos(angle_rad), math.sin(angle_rad)]


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors_return_one(self) -> None:
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-9)

    def test_identical_vectors_return_one_small(self) -> None:
        v = [0.5, 0.5]
        assert cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-9)

    def test_orthogonal_vectors_return_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0, abs=1e-9)

    def test_antiparallel_vectors_return_minus_one(self) -> None:
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0, abs=1e-9)

    def test_known_angle_45_degrees(self) -> None:
        a = [1.0, 0.0]
        b = [1.0, 1.0]  # 45° — cosine = 1/√2
        assert cosine_similarity(a, b) == pytest.approx(1.0 / math.sqrt(2), abs=1e-9)

    def test_empty_a_returns_zero(self) -> None:
        assert cosine_similarity([], [1.0, 2.0]) == 0.0

    def test_empty_b_returns_zero(self) -> None:
        assert cosine_similarity([1.0, 2.0], []) == 0.0

    def test_both_empty_returns_zero(self) -> None:
        assert cosine_similarity([], []) == 0.0

    def test_unequal_length_returns_zero(self) -> None:
        assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0

    def test_zero_vector_a_returns_zero(self) -> None:
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_zero_vector_b_returns_zero(self) -> None:
        assert cosine_similarity([1.0, 1.0], [0.0, 0.0]) == 0.0

    def test_both_zero_vectors_return_zero(self) -> None:
        assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# disabled cache
# ---------------------------------------------------------------------------


class TestSemanticCacheDisabled:
    def test_enabled_property_false(self) -> None:
        cache = SemanticCache(enabled=False)
        assert cache.enabled is False

    def test_lookup_returns_none_when_disabled(self) -> None:
        cache = SemanticCache(enabled=False)
        cache.store("k", [1.0, 0.0], {"answer": 42})  # would be stored if enabled
        assert cache.lookup([1.0, 0.0]) is None

    def test_store_does_not_increment_size(self) -> None:
        cache = SemanticCache(enabled=False)
        cache.store("k", [1.0, 0.0], {"answer": 42})
        assert cache.size == 0

    def test_size_zero_after_multiple_stores(self) -> None:
        cache = SemanticCache(enabled=False)
        for i in range(5):
            cache.store(f"k{i}", [float(i), 0.0], {})
        assert cache.size == 0


# ---------------------------------------------------------------------------
# store + lookup round-trip
# ---------------------------------------------------------------------------


class TestSemanticCacheRoundTrip:
    def test_identical_embedding_returns_response(self) -> None:
        cache = SemanticCache(enabled=True, threshold=0.95)
        emb = [1.0, 0.0, 0.0]
        resp = {"choices": [{"message": {"content": "hello"}}]}
        cache.store("req-1", emb, resp)
        result = cache.lookup(emb)
        assert result == resp

    def test_lookup_returns_dict_not_copy(self) -> None:
        """The exact dict object (or equal contents) is returned."""
        cache = SemanticCache(enabled=True, threshold=0.9)
        resp = {"id": "chatcmpl-xyz"}
        cache.store("k", [1.0, 0.0], resp)
        assert cache.lookup([1.0, 0.0]) == resp

    def test_empty_cache_returns_none(self) -> None:
        cache = SemanticCache(enabled=True)
        assert cache.lookup([1.0, 0.0]) is None

    def test_size_increments_after_store(self) -> None:
        cache = SemanticCache(enabled=True)
        cache.store("k1", [1.0, 0.0], {})
        cache.store("k2", [0.0, 1.0], {})
        assert cache.size == 2

    def test_clear_resets_size(self) -> None:
        cache = SemanticCache(enabled=True)
        cache.store("k", [1.0, 0.0], {})
        cache.clear()
        assert cache.size == 0

    def test_clear_lookup_returns_none(self) -> None:
        cache = SemanticCache(enabled=True, threshold=0.9)
        emb = [1.0, 0.0]
        cache.store("k", emb, {"data": 1})
        cache.clear()
        assert cache.lookup(emb) is None


# ---------------------------------------------------------------------------
# threshold boundary
# ---------------------------------------------------------------------------


class TestSemanticCacheThreshold:
    """Use 2-D unit vectors so cosine similarity is exactly cos(θ)."""

    def test_above_threshold_matches(self) -> None:
        # angle = 5°, cos(5°) ≈ 0.9962  →  threshold=0.99 → hit
        theta = math.radians(5)
        cache = SemanticCache(enabled=True, threshold=0.99)
        cache.store("k", _unit(0.0), {"hit": True})
        assert cache.lookup(_unit(theta)) == {"hit": True}

    def test_below_threshold_no_match(self) -> None:
        # angle = 15°, cos(15°) ≈ 0.9659  →  threshold=0.99 → miss
        theta = math.radians(15)
        cache = SemanticCache(enabled=True, threshold=0.99)
        cache.store("k", _unit(0.0), {"hit": True})
        assert cache.lookup(_unit(theta)) is None

    def test_exact_threshold_matches(self) -> None:
        # threshold == similarity → hit (>=)
        cache = SemanticCache(enabled=True, threshold=1.0)
        emb = [1.0, 0.0]
        cache.store("k", emb, {"exact": True})
        result = cache.lookup(emb)
        assert result == {"exact": True}

    def test_threshold_09_matches_close_vector(self) -> None:
        # angle = 20°, cos(20°) ≈ 0.9397  →  threshold=0.90 → hit
        theta = math.radians(20)
        cache = SemanticCache(enabled=True, threshold=0.90)
        cache.store("k", _unit(0.0), {"ok": 1})
        assert cache.lookup(_unit(theta)) is not None


# ---------------------------------------------------------------------------
# nearest-neighbour: multiple entries, best wins
# ---------------------------------------------------------------------------


class TestSemanticCacheNearestNeighbour:
    def test_nearest_entry_returned(self) -> None:
        cache = SemanticCache(enabled=True, threshold=0.0)
        # Store three unit vectors at 0°, 30°, 60°
        cache.store("0deg", _unit(0.0), {"angle": 0})
        cache.store("30deg", _unit(math.radians(30)), {"angle": 30})
        cache.store("60deg", _unit(math.radians(60)), {"angle": 60})

        # Query at 25° — closest to 30°
        result = cache.lookup(_unit(math.radians(25)))
        assert result == {"angle": 30}

    def test_query_close_to_first_entry(self) -> None:
        cache = SemanticCache(enabled=True, threshold=0.0)
        cache.store("a", _unit(0.0), {"val": "a"})
        cache.store("b", _unit(math.radians(80)), {"val": "b"})

        # Query at 5° — closest to 0°
        assert cache.lookup(_unit(math.radians(5))) == {"val": "a"}


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


class TestSemanticCacheLRUEviction:
    def test_oldest_evicted_when_full(self) -> None:
        cache = SemanticCache(enabled=True, max_entries=2, threshold=0.99)
        cache.store("a", [1.0, 0.0, 0.0], {"id": "a"})
        cache.store("b", [0.0, 1.0, 0.0], {"id": "b"})
        cache.store("c", [0.0, 0.0, 1.0], {"id": "c"})  # "a" should be evicted

        assert cache.size == 2
        # "a" was least-recently-used → gone
        assert cache.lookup([1.0, 0.0, 0.0]) is None
        # "b" and "c" still present
        assert cache.lookup([0.0, 1.0, 0.0]) == {"id": "b"}
        assert cache.lookup([0.0, 0.0, 1.0]) == {"id": "c"}

    def test_lookup_promotes_entry_to_mru(self) -> None:
        cache = SemanticCache(enabled=True, max_entries=2, threshold=0.99)
        cache.store("a", [1.0, 0.0, 0.0], {"id": "a"})
        cache.store("b", [0.0, 1.0, 0.0], {"id": "b"})

        # Access "a" — makes it MRU; "b" becomes LRU
        _ = cache.lookup([1.0, 0.0, 0.0])

        # Store "c" — "b" should be evicted (LRU), not "a"
        cache.store("c", [0.0, 0.0, 1.0], {"id": "c"})

        assert cache.size == 2
        assert cache.lookup([1.0, 0.0, 0.0]) == {"id": "a"}
        assert cache.lookup([0.0, 1.0, 0.0]) is None  # "b" evicted
        assert cache.lookup([0.0, 0.0, 1.0]) == {"id": "c"}

    def test_max_entries_1_always_evicts_previous(self) -> None:
        cache = SemanticCache(enabled=True, max_entries=1, threshold=0.99)
        cache.store("a", [1.0, 0.0], {"id": "a"})
        cache.store("b", [0.0, 1.0], {"id": "b"})

        assert cache.size == 1
        assert cache.lookup([0.0, 1.0]) == {"id": "b"}


# ---------------------------------------------------------------------------
# TTL expiry (fake clock — NO real sleeps)
# ---------------------------------------------------------------------------


class TestSemanticCacheTTL:
    def test_lookup_before_expiry_returns_hit(self) -> None:
        t, now = _fake_clock(start=1_000.0)
        cache = SemanticCache(enabled=True, threshold=0.99, ttl_seconds=10, now=now)
        emb = [1.0, 0.0]
        cache.store("k", emb, {"fresh": True})

        t[0] = 1_009.0  # 9 s elapsed — before TTL (10 s)
        assert cache.lookup(emb) == {"fresh": True}

    def test_lookup_after_expiry_returns_none(self) -> None:
        t, now = _fake_clock(start=1_000.0)
        cache = SemanticCache(enabled=True, threshold=0.99, ttl_seconds=10, now=now)
        emb = [1.0, 0.0]
        cache.store("k", emb, {"fresh": True})

        t[0] = 1_011.0  # 11 s elapsed — past TTL
        assert cache.lookup(emb) is None

    def test_lookup_at_exact_expiry_boundary_returns_none(self) -> None:
        t, now = _fake_clock(start=1_000.0)
        cache = SemanticCache(enabled=True, threshold=0.99, ttl_seconds=10, now=now)
        emb = [1.0, 0.0]
        cache.store("k", emb, {"val": 1})

        t[0] = 1_010.0  # elapsed == ttl → expired (>=)
        assert cache.lookup(emb) is None

    def test_expired_entry_removed_from_size(self) -> None:
        t, now = _fake_clock(start=1_000.0)
        cache = SemanticCache(enabled=True, threshold=0.99, ttl_seconds=5, now=now)
        cache.store("k", [1.0, 0.0], {"v": 1})

        t[0] = 1_010.0  # expired
        cache.lookup([1.0, 0.0])  # triggers eviction scan
        assert cache.size == 0

    def test_ttl_zero_means_no_expiry(self) -> None:
        t, now = _fake_clock(start=1_000.0)
        cache = SemanticCache(enabled=True, threshold=0.99, ttl_seconds=0, now=now)
        emb = [1.0, 0.0]
        cache.store("k", emb, {"immortal": True})

        t[0] = 999_999.0  # far future
        assert cache.lookup(emb) == {"immortal": True}

    def test_only_expired_entries_removed_live_entries_survive(self) -> None:
        t, now = _fake_clock(start=1_000.0)
        cache = SemanticCache(enabled=True, threshold=0.99, ttl_seconds=10, now=now)
        cache.store("old", [1.0, 0.0, 0.0], {"age": "old"})

        t[0] = 1_005.0  # store a second entry later
        cache.store("new", [0.0, 1.0, 0.0], {"age": "new"})

        t[0] = 1_011.0  # "old" expired (1000+10), "new" not (1005+10=1015)
        assert cache.lookup([1.0, 0.0, 0.0]) is None
        assert cache.lookup([0.0, 1.0, 0.0]) == {"age": "new"}


# ---------------------------------------------------------------------------
# empty embedding guard
# ---------------------------------------------------------------------------


class TestSemanticCacheEmptyEmbedding:
    def test_lookup_empty_embedding_returns_none(self) -> None:
        cache = SemanticCache(enabled=True, threshold=0.9)
        cache.store("k", [1.0, 0.0], {"ok": True})
        assert cache.lookup([]) is None

    def test_store_empty_embedding_is_noop(self) -> None:
        cache = SemanticCache(enabled=True)
        cache.store("k", [], {"ok": True})
        assert cache.size == 0

    def test_store_empty_then_real_lookup_returns_real(self) -> None:
        cache = SemanticCache(enabled=True, threshold=0.99)
        cache.store("noop", [], {"bad": True})  # should be ignored
        cache.store("real", [1.0, 0.0], {"good": True})
        assert cache.lookup([1.0, 0.0]) == {"good": True}
