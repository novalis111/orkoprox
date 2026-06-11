"""Tests for monthly quota enforcement.

Tests:
1. ``KeyConfig`` derives monthly limit from daily when not explicitly set
2. ``_next_month_first_iso`` — reset date calculation (first of next month)
3. ``build_usage_headers`` mit Monthly-Pendants
4. ``check_budget`` mit Monthly-Hard-Stop
5. Combined-Pct = max(daily, monthly)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.token_metering import (
    DEFAULT_DAILY_TO_MONTHLY_FACTOR,
    DailyUsage,
    KeyConfig,
    TokenMeteringService,
    _current_month_key,
    _next_month_first_iso,
)


class TestKeyConfigMonthly:
    """KeyConfig V4: monthly_token_limit + auto-derive."""

    def test_explicit_monthly_limit(self) -> None:
        cfg = KeyConfig(
            tenant_id="t1",
            daily_token_limit=500_000,
            monthly_token_limit=10_000_000,
        )
        assert cfg.monthly_token_limit == 10_000_000

    def test_auto_derive_monthly_from_daily(self) -> None:
        # Wenn nur daily gesetzt: monthly = daily * 30
        cfg = KeyConfig(tenant_id="t1", daily_token_limit=1_000_000)
        assert cfg.monthly_token_limit == 1_000_000 * DEFAULT_DAILY_TO_MONTHLY_FACTOR

    def test_no_limits_means_no_monthly(self) -> None:
        cfg = KeyConfig(tenant_id="t1")
        assert cfg.daily_token_limit == 0
        assert cfg.monthly_token_limit == 0

    def test_monthly_only_without_daily(self) -> None:
        # Nur monthly gesetzt: daily=0 (unlimited daily, monthly enforced)
        cfg = KeyConfig(tenant_id="t1", monthly_token_limit=10_000_000)
        assert cfg.daily_token_limit == 0
        assert cfg.monthly_token_limit == 10_000_000

    def test_serialize_roundtrip(self) -> None:
        cfg = KeyConfig(
            tenant_id="t1",
            daily_token_limit=500_000,
            monthly_token_limit=10_000_000,
            package="solo",
        )
        d = cfg.to_dict()
        assert d["monthly_token_limit"] == 10_000_000
        cfg2 = KeyConfig.from_dict(d)
        assert cfg2.monthly_token_limit == 10_000_000
        assert cfg2.daily_token_limit == 500_000


class TestNextMonthFirstIso:
    """Reset-Berechnung: immer 1. des Folgemonats 00:00 UTC."""

    def test_mid_month(self) -> None:
        now = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
        assert _next_month_first_iso(now) == "2026-05-01T00:00:00Z"

    def test_just_before_month_end(self) -> None:
        now = datetime(2026, 4, 30, 23, 59, 59, tzinfo=UTC)
        assert _next_month_first_iso(now) == "2026-05-01T00:00:00Z"

    def test_first_of_month(self) -> None:
        now = datetime(2026, 5, 1, 0, 0, 1, tzinfo=UTC)
        assert _next_month_first_iso(now) == "2026-06-01T00:00:00Z"

    def test_december_rollover(self) -> None:
        now = datetime(2026, 12, 15, 12, 0, 0, tzinfo=UTC)
        assert _next_month_first_iso(now) == "2027-01-01T00:00:00Z"

    def test_december_last_second(self) -> None:
        now = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
        assert _next_month_first_iso(now) == "2027-01-01T00:00:00Z"

    def test_format_uses_z_suffix(self) -> None:
        result = _next_month_first_iso()
        assert result.endswith("Z")
        assert "+00:00" not in result


class TestCurrentMonthKey:
    def test_format(self) -> None:
        # Format YYYY-MM (Redis-Key-Komponente)
        result = _current_month_key(datetime(2026, 5, 15, tzinfo=UTC))
        assert result == "2026-05"

    def test_january(self) -> None:
        result = _current_month_key(datetime(2026, 1, 1, tzinfo=UTC))
        assert result == "2026-01"


class TestMonthlyHeaders:
    """build_usage_headers liefert Monthly-Pendants."""

    def _service(self) -> TokenMeteringService:
        return TokenMeteringService(redis_client=None)

    def test_monthly_headers_present(self) -> None:
        svc = self._service()
        cfg = KeyConfig(
            tenant_id="t1",
            daily_token_limit=500_000,
            monthly_token_limit=10_000_000,
        )
        daily = DailyUsage(total_tokens=100_000)
        monthly = DailyUsage(total_tokens=2_500_000, cost_micro_usd=500_000)
        headers = svc.build_usage_headers(cfg, daily, monthly=monthly)
        assert headers["X-Orkoprox-Tokens-Used-Month"] == "2500000"
        assert headers["X-Orkoprox-Token-Limit-Month"] == "10000000"
        assert headers["X-Orkoprox-Usage-Pct-Month"] == "25"
        assert headers["X-Orkoprox-Quota-Reset-Month"].endswith("Z")
        # Monthly-Cost separat ausgewiesen
        assert headers["X-Orkoprox-Cost-USD-Month"] == "0.5000"

    def test_combined_pct_takes_max(self) -> None:
        # Daily 50%, Monthly 80% => combined 80% (warn)
        svc = self._service()
        cfg = KeyConfig(
            tenant_id="t1",
            daily_token_limit=1_000_000,
            monthly_token_limit=10_000_000,
        )
        daily = DailyUsage(total_tokens=500_000)  # 50%
        monthly = DailyUsage(total_tokens=8_000_000)  # 80%
        headers = svc.build_usage_headers(cfg, daily, monthly=monthly)
        assert headers["X-Orkoprox-Daily-Pct"] == "50"
        assert headers["X-Orkoprox-Usage-Pct-Month"] == "80"
        # combined = max = 80
        assert headers["X-Orkoprox-Usage-Pct"] == "80"
        assert headers["X-Orkoprox-Quota-Status"] == "warn"

    def test_monthly_exceeded_overrides_daily(self) -> None:
        # Daily ok, Monthly 100% => combined exceeded
        svc = self._service()
        cfg = KeyConfig(
            tenant_id="t1",
            daily_token_limit=1_000_000,
            monthly_token_limit=10_000_000,
        )
        daily = DailyUsage(total_tokens=100_000)  # 10%
        monthly = DailyUsage(total_tokens=10_000_000)  # 100%
        headers = svc.build_usage_headers(cfg, daily, monthly=monthly)
        assert headers["X-Orkoprox-Quota-Status"] == "exceeded"

    def test_monthly_only_without_daily_limit(self) -> None:
        # Nur Monthly gesetzt: daily-pct=0, monthly enforced
        svc = self._service()
        cfg = KeyConfig(tenant_id="t1", monthly_token_limit=10_000_000)
        daily = DailyUsage(total_tokens=999_999)
        monthly = DailyUsage(total_tokens=9_500_000)  # 95%
        headers = svc.build_usage_headers(cfg, daily, monthly=monthly)
        assert headers["X-Orkoprox-Quota-Status"] == "critical"
        assert headers["X-Orkoprox-Daily-Pct"] == "0"


class TestCheckBudgetMonthly:
    """check_budget enforced Monthly als primaeren Hard-Stop."""

    def _make_service_with_redis_mock(
        self,
        config_dict: dict,
        daily_total: int,
        monthly_total: int,
    ) -> tuple[TokenMeteringService, MagicMock]:
        import json

        redis_mock = MagicMock()

        def _get(key: str) -> str | None:
            if key.startswith("metering:config:"):
                return json.dumps(config_dict)
            return None

        def _hgetall(key: str) -> dict:
            # Nimmt an: monthly = key endet auf YYYY-MM, daily endet auf YYYY-MM-DD.
            if len(key.rsplit(":", 1)[-1]) == 7:  # YYYY-MM
                return {"total_tokens": str(monthly_total), "request_count": "1"}
            return {"total_tokens": str(daily_total), "request_count": "1"}

        redis_mock.get.side_effect = _get
        redis_mock.hgetall.side_effect = _hgetall
        svc = TokenMeteringService(redis_client=redis_mock)
        return svc, redis_mock

    def test_allowed_when_both_under_limit(self) -> None:
        cfg = {
            "tenant_id": "t1",
            "daily_token_limit": 500_000,
            "monthly_token_limit": 10_000_000,
            "active": True,
            "package": "solo",
        }
        svc, _ = self._make_service_with_redis_mock(cfg, daily_total=100_000, monthly_total=2_000_000)
        allowed, config, _ = svc.check_budget("test_key")
        assert allowed
        assert config is not None

    def test_blocked_when_monthly_exceeded(self) -> None:
        cfg = {
            "tenant_id": "t1",
            "daily_token_limit": 500_000,
            "monthly_token_limit": 10_000_000,
            "active": True,
            "package": "solo",
        }
        svc, _ = self._make_service_with_redis_mock(
            cfg,
            daily_total=100_000,  # daily ok
            monthly_total=10_000_000,  # monthly exceeded
        )
        allowed, config, _ = svc.check_budget("test_key")
        assert not allowed
        assert config is not None

    def test_blocked_when_daily_exceeded(self) -> None:
        cfg = {
            "tenant_id": "t1",
            "daily_token_limit": 500_000,
            "monthly_token_limit": 10_000_000,
            "active": True,
            "package": "solo",
        }
        svc, _ = self._make_service_with_redis_mock(
            cfg,
            daily_total=500_000,  # daily exceeded
            monthly_total=2_000_000,  # monthly ok
        )
        allowed, _, _ = svc.check_budget("test_key")
        assert not allowed

    def test_unmetered_key_always_allowed(self) -> None:
        cfg = {
            "tenant_id": "t1",
            "daily_token_limit": 0,
            "monthly_token_limit": 0,
            "active": True,
            "package": "test",
        }
        svc, _ = self._make_service_with_redis_mock(cfg, daily_total=999_999, monthly_total=999_999)
        allowed, _, _ = svc.check_budget("test_key")
        assert allowed

    def test_inactive_key_blocked(self) -> None:
        cfg = {
            "tenant_id": "t1",
            "daily_token_limit": 500_000,
            "monthly_token_limit": 10_000_000,
            "active": False,
            "package": "solo",
        }
        svc, _ = self._make_service_with_redis_mock(cfg, daily_total=0, monthly_total=0)
        allowed, _, _ = svc.check_budget("test_key")
        assert not allowed


class TestKeyConfigBootstrapV4:
    """Bootstrap-Skript-Tier-Resolution."""

    def test_solo_tier_resolution(self) -> None:
        from scripts.keyconfig_bootstrap import resolve_tier_quota

        daily, monthly = resolve_tier_quota(package="solo")
        assert monthly == 10_000_000

    def test_business_tier_resolution(self) -> None:
        from scripts.keyconfig_bootstrap import resolve_tier_quota

        daily, monthly = resolve_tier_quota(package="business")
        assert monthly == 40_000_000

    def test_professional_tier_resolution(self) -> None:
        from scripts.keyconfig_bootstrap import resolve_tier_quota

        daily, monthly = resolve_tier_quota(package="professional")
        assert monthly == 120_000_000

    def test_enterprise_tier_resolution(self) -> None:
        from scripts.keyconfig_bootstrap import resolve_tier_quota

        daily, monthly = resolve_tier_quota(package="enterprise")
        assert monthly == 400_000_000

    def test_tier_override_priority(self) -> None:
        from scripts.keyconfig_bootstrap import resolve_tier_quota

        # tier_override gewinnt ueber package
        daily, monthly = resolve_tier_quota(package="solo", tier_override="enterprise")
        assert monthly == 400_000_000

    def test_unknown_tier_fallback(self) -> None:
        from scripts.keyconfig_bootstrap import resolve_tier_quota

        daily, monthly = resolve_tier_quota(package="unknown-pkg", daily_fallback=1_000_000)
        assert daily == 1_000_000
        assert monthly == 30_000_000  # daily * 30


class TestRecordUsageMonthlyAggregate:
    """record_usage schreibt parallel in Daily und Monthly Redis-Keys."""

    @pytest.fixture
    def redis_mock(self) -> MagicMock:
        m = MagicMock()
        # Pipeline mock returns predictable values
        pipe = MagicMock()
        pipe.execute.return_value = [100, 200, 300, 1, 100, 200, 300, 1]
        m.pipeline.return_value = pipe
        m.hget.side_effect = lambda k, f: 0
        # get_key_config liest Redis.get -> wir liefern None (= unmetered Key)
        # damit get_key_config keinen JSON-Parse braucht.
        m.get.return_value = None
        return m

    def test_writes_to_both_daily_and_monthly_keys(self, redis_mock: MagicMock) -> None:
        svc = TokenMeteringService(redis_client=redis_mock)
        svc.record_usage(
            "test_key",
            prompt_tokens=100,
            completion_tokens=200,
            model="some-non-ovh-model",
            provider="other",
        )
        # Pipeline wurde gebaut + executed
        pipe = redis_mock.pipeline.return_value
        # Mind. 8 hincrby-Calls (4 daily + 4 monthly fields)
        assert pipe.hincrby.call_count >= 8
        # Mind. 2 expire (daily + monthly)
        assert pipe.expire.call_count >= 2
