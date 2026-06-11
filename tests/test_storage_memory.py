"""Unit tests for MemoryStore — the Zero-Config in-process KeyValueStore."""

from __future__ import annotations


from app.storage import MemoryStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_clock(start: float = 1_000.0) -> tuple[list[float], "callable[[], float]"]:
    """Return a mutable clock state list and a callable that reads it."""
    t = [start]

    def now() -> float:
        return t[0]

    return t, now


# ---------------------------------------------------------------------------
# String operations
# ---------------------------------------------------------------------------


class TestMemoryStoreStrings:
    def test_set_get_roundtrip(self) -> None:
        store = MemoryStore()
        store.set_str("k", "hello")
        assert store.get_str("k") == "hello"

    def test_get_missing_returns_none(self) -> None:
        store = MemoryStore()
        assert store.get_str("no-such-key") is None

    def test_delete_removes_key(self) -> None:
        store = MemoryStore()
        store.set_str("k", "v")
        store.delete("k")
        assert store.get_str("k") is None

    def test_delete_nonexistent_is_no_op(self) -> None:
        store = MemoryStore()
        store.delete("ghost")  # must not raise

    def test_overwrite_string(self) -> None:
        store = MemoryStore()
        store.set_str("k", "first")
        store.set_str("k", "second")
        assert store.get_str("k") == "second"


# ---------------------------------------------------------------------------
# Field counters (incr_fields / get_fields)
# ---------------------------------------------------------------------------


class TestMemoryStoreFields:
    def test_incr_fields_creates_and_returns(self) -> None:
        store = MemoryStore()
        result = store.incr_fields("bucket", {"a": 3, "b": 7}, ttl_seconds=300)
        assert result == {"a": 3, "b": 7}

    def test_incr_fields_accumulates(self) -> None:
        store = MemoryStore()
        store.incr_fields("bucket", {"x": 10}, ttl_seconds=300)
        result = store.incr_fields("bucket", {"x": 5}, ttl_seconds=300)
        assert result["x"] == 15

    def test_incr_fields_returns_full_map(self) -> None:
        """Fields not in the second call still appear in the returned map."""
        store = MemoryStore()
        store.incr_fields("bucket", {"a": 1, "b": 2}, ttl_seconds=300)
        result = store.incr_fields("bucket", {"a": 10}, ttl_seconds=300)
        assert result["a"] == 11
        assert result["b"] == 2  # old field preserved

    def test_get_fields_missing_key_returns_empty(self) -> None:
        store = MemoryStore()
        assert store.get_fields("nothing") == {}

    def test_get_fields_after_incr(self) -> None:
        store = MemoryStore()
        store.incr_fields("bucket", {"total": 100, "req": 1}, ttl_seconds=300)
        fields = store.get_fields("bucket")
        assert fields["total"] == 100
        assert fields["req"] == 1

    def test_delete_removes_fields(self) -> None:
        store = MemoryStore()
        store.incr_fields("bucket", {"x": 1}, ttl_seconds=300)
        store.delete("bucket")
        assert store.get_fields("bucket") == {}


# ---------------------------------------------------------------------------
# scan_prefix
# ---------------------------------------------------------------------------


class TestMemoryStoreScanPrefix:
    def test_finds_keys_with_prefix(self) -> None:
        store = MemoryStore()
        store.set_str("ns:a", "1")
        store.set_str("ns:b", "2")
        store.set_str("other:c", "3")
        found = store.scan_prefix("ns:")
        assert set(found) == {"ns:a", "ns:b"}

    def test_finds_field_keys(self) -> None:
        store = MemoryStore()
        store.incr_fields("ns:counters:x", {"n": 1}, ttl_seconds=300)
        found = store.scan_prefix("ns:")
        assert "ns:counters:x" in found

    def test_no_match_returns_empty(self) -> None:
        store = MemoryStore()
        store.set_str("alpha:1", "v")
        assert store.scan_prefix("beta:") == []

    def test_multiple_keys_found(self) -> None:
        store = MemoryStore()
        for i in range(5):
            store.set_str(f"pfx:{i}", str(i))
        store.set_str("unrelated", "x")
        found = store.scan_prefix("pfx:")
        assert len(found) == 5
        assert "unrelated" not in found


# ---------------------------------------------------------------------------
# TTL expiry (fake clock — NO real sleeps)
# ---------------------------------------------------------------------------


class TestMemoryStoreTTL:
    def test_fields_available_before_expiry(self) -> None:
        t, now = _fake_clock(start=1000.0)
        store = MemoryStore(now=now)
        store.incr_fields("key", {"n": 5}, ttl_seconds=10)

        t[0] = 1009.0  # just before expiry (1000 + 10 = 1010)
        assert store.get_fields("key") == {"n": 5}

    def test_fields_evicted_after_expiry(self) -> None:
        t, now = _fake_clock(start=1000.0)
        store = MemoryStore(now=now)
        store.incr_fields("key", {"n": 5}, ttl_seconds=10)

        t[0] = 1011.0  # past expiry
        assert store.get_fields("key") == {}

    def test_fields_evicted_at_exact_expiry_boundary(self) -> None:
        t, now = _fake_clock(start=1000.0)
        store = MemoryStore(now=now)
        store.incr_fields("key", {"n": 5}, ttl_seconds=10)

        t[0] = 1010.0  # at exact expiry (now() >= expiry → expired)
        assert store.get_fields("key") == {}

    def test_ttl_refresh_on_second_incr(self) -> None:
        t, now = _fake_clock(start=1000.0)
        store = MemoryStore(now=now)
        store.incr_fields("key", {"n": 1}, ttl_seconds=10)

        t[0] = 1008.0  # close to expiry but not yet
        store.incr_fields("key", {"n": 1}, ttl_seconds=10)  # refresh TTL

        t[0] = 1017.0  # would have expired under old TTL, not under new
        assert store.get_fields("key") == {"n": 2}

        t[0] = 1019.0  # past refreshed TTL (1008 + 10 = 1018)
        assert store.get_fields("key") == {}

    def test_string_values_never_expire(self) -> None:
        """set_str does not set a TTL — strings survive time advancing."""
        t, now = _fake_clock(start=1000.0)
        store = MemoryStore(now=now)
        store.set_str("cfg:key", "persistent")

        t[0] = 9_999_999.0  # far future
        assert store.get_str("cfg:key") == "persistent"

    def test_scan_prefix_evicts_expired_before_scan(self) -> None:
        t, now = _fake_clock(start=1000.0)
        store = MemoryStore(now=now)
        store.incr_fields("ns:expired", {"n": 1}, ttl_seconds=10)
        store.set_str("ns:permanent", "x")

        t[0] = 1020.0  # ns:expired is past TTL
        found = store.scan_prefix("ns:")
        assert "ns:expired" not in found
        assert "ns:permanent" in found
