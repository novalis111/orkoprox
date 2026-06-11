"""Tests for TokenMeteringService in Zero-Config mode (no Redis, in-memory store).

The key property: TokenMeteringService() with no arguments uses MemoryStore
internally. Metering always works — .enabled is always True.
"""

from __future__ import annotations


from app.storage import MemoryStore
from app.token_metering import DailyUsage, KeyConfig, TokenMeteringService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(**kwargs: object) -> TokenMeteringService:
    """Return a fresh TokenMeteringService backed by an in-process MemoryStore."""
    return TokenMeteringService(**kwargs)  # type: ignore[arg-type]


def _register(svc: TokenMeteringService, key: str = "test-key-001", **kw: object) -> KeyConfig:
    cfg = KeyConfig(tenant_id=kw.pop("tenant_id", "tenant-a"), **kw)  # type: ignore[arg-type]
    svc.register_key(key, cfg)
    return cfg


# ---------------------------------------------------------------------------
# Zero-config basics
# ---------------------------------------------------------------------------


class TestZeroConfigEnabled:
    def test_no_args_enabled(self) -> None:
        svc = TokenMeteringService()
        assert svc.enabled is True

    def test_explicit_store_still_enabled(self) -> None:
        svc = TokenMeteringService(store=MemoryStore())
        assert svc.enabled is True

    def test_redis_client_none_still_enabled(self) -> None:
        svc = TokenMeteringService(redis_client=None)
        assert svc.enabled is True


# ---------------------------------------------------------------------------
# register_key / get_key_config roundtrip
# ---------------------------------------------------------------------------


class TestRegisterGetKey:
    def test_register_then_get(self) -> None:
        svc = _service()
        cfg = KeyConfig(tenant_id="tenant-x", daily_token_limit=1000, package="basic")
        svc.register_key("api-key-1", cfg)
        retrieved = svc.get_key_config("api-key-1")
        assert retrieved is not None
        assert retrieved.tenant_id == "tenant-x"
        assert retrieved.daily_token_limit == 1000
        assert retrieved.package == "basic"

    def test_unknown_key_returns_none(self) -> None:
        svc = _service()
        assert svc.get_key_config("nonexistent") is None

    def test_monthly_auto_derived_from_daily(self) -> None:
        svc = _service()
        cfg = KeyConfig(tenant_id="t", daily_token_limit=10_000)
        svc.register_key("k", cfg)
        retrieved = svc.get_key_config("k")
        assert retrieved is not None
        assert retrieved.monthly_token_limit == 10_000 * 30


# ---------------------------------------------------------------------------
# record_usage accumulation
# ---------------------------------------------------------------------------


class TestRecordUsage:
    def test_record_single_call_shows_in_daily(self) -> None:
        svc = _service()
        _register(svc, "k")
        svc.record_usage("k", prompt_tokens=100, completion_tokens=50)
        usage = svc.get_daily_usage("k")
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.request_count == 1

    def test_record_multiple_calls_accumulate(self) -> None:
        svc = _service()
        _register(svc, "k")
        svc.record_usage("k", prompt_tokens=100, completion_tokens=50)
        svc.record_usage("k", prompt_tokens=200, completion_tokens=100)
        usage = svc.get_daily_usage("k")
        assert usage.prompt_tokens == 300
        assert usage.completion_tokens == 150
        assert usage.request_count == 2

    def test_record_usage_also_updates_monthly(self) -> None:
        svc = _service()
        _register(svc, "k")
        svc.record_usage("k", prompt_tokens=500, completion_tokens=200)
        daily = svc.get_daily_usage("k")
        monthly = svc.get_monthly_usage("k")
        # Monthly mirrors daily within same month
        assert monthly.prompt_tokens == daily.prompt_tokens
        assert monthly.completion_tokens == daily.completion_tokens
        assert monthly.request_count == daily.request_count

    def test_record_usage_ovh_model_produces_cost(self) -> None:
        """Using a priced OVH model (via provider='ovh') results in cost_micro_usd > 0."""
        svc = _service()
        _register(svc, "k")
        svc.record_usage(
            "k",
            provider="ovh",
            model="Mistral-Small-3.2-24B-Instruct-2506",
            prompt_tokens=1000,
            completion_tokens=500,
        )
        daily = svc.get_daily_usage("k")
        assert daily.cost_micro_usd > 0

    def test_record_usage_unknown_model_no_cost(self) -> None:
        """An unrecognised model with no provider tag accumulates 0 cost."""
        svc = _service()
        _register(svc, "k")
        svc.record_usage("k", provider="unknown", model="no-such-model", prompt_tokens=500)
        daily = svc.get_daily_usage("k")
        assert daily.cost_micro_usd == 0

    def test_unregistered_key_record_still_works(self) -> None:
        """record_usage does not crash for an unregistered (unmetered) key."""
        svc = _service()
        usage = svc.record_usage("mystery-key", prompt_tokens=10, completion_tokens=5)
        assert isinstance(usage, DailyUsage)


# ---------------------------------------------------------------------------
# list_all_keys (relies on scan_prefix via MemoryStore)
# ---------------------------------------------------------------------------


class TestListAllKeys:
    def test_registered_key_appears_in_list(self) -> None:
        svc = _service()
        _register(svc, "long-api-key-for-listing", tenant_id="tenant-list")
        keys = svc.list_all_keys()
        tenant_ids = [k["tenant_id"] for k in keys]
        assert "tenant-list" in tenant_ids

    def test_multiple_keys_all_listed(self) -> None:
        svc = _service()
        for i in range(3):
            _register(svc, f"api-key-{i:03}", tenant_id=f"tenant-{i}")
        keys = svc.list_all_keys()
        assert len(keys) == 3

    def test_empty_service_returns_empty_list(self) -> None:
        svc = _service()
        assert svc.list_all_keys() == []


# ---------------------------------------------------------------------------
# check_budget enforcement
# ---------------------------------------------------------------------------


class TestCheckBudget:
    def test_under_limit_is_allowed(self) -> None:
        svc = _service()
        _register(svc, "k", daily_token_limit=10_000)
        svc.record_usage("k", prompt_tokens=100, completion_tokens=50)
        allowed, cfg, _usage = svc.check_budget("k")
        assert allowed is True
        assert cfg is not None

    def test_over_daily_limit_is_blocked(self) -> None:
        svc = _service()
        _register(svc, "k", daily_token_limit=100)
        # Each record_usage increments total_tokens by prompt+completion
        svc.record_usage("k", prompt_tokens=60, completion_tokens=50)  # 110 total > 100
        allowed, cfg, _usage = svc.check_budget("k")
        assert allowed is False
        assert cfg is not None

    def test_unregistered_key_always_allowed(self) -> None:
        svc = _service()
        allowed, cfg, _usage = svc.check_budget("no-such-key")
        assert allowed is True
        assert cfg is None

    def test_inactive_key_is_blocked(self) -> None:
        svc = _service()
        cfg = KeyConfig(tenant_id="t", daily_token_limit=10_000, active=False)
        svc.register_key("k", cfg)
        allowed, _, _usage = svc.check_budget("k")
        assert allowed is False

    def test_zero_limits_is_unmetered(self) -> None:
        """daily=0 + monthly=0 means pass-through (test / internal keys)."""
        svc = _service()
        _register(svc, "k", daily_token_limit=0, monthly_token_limit=0)
        allowed, cfg, _usage = svc.check_budget("k")
        assert allowed is True


# ---------------------------------------------------------------------------
# Isolation: two separate service instances do NOT share state
# ---------------------------------------------------------------------------


class TestIsolatedInstances:
    def test_two_services_independent_state(self) -> None:
        """Each TokenMeteringService() gets its own MemoryStore — state is isolated."""
        svc_a = _service()
        svc_b = _service()

        _register(svc_a, "shared-key", tenant_id="tenant-a")
        svc_a.record_usage("shared-key", prompt_tokens=200, completion_tokens=100)

        # svc_b knows nothing about "shared-key"
        assert svc_b.get_key_config("shared-key") is None
        assert svc_b.get_daily_usage("shared-key").total_tokens == 0
