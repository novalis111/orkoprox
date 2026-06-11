"""Tests fuer Soft-Warn/Hard-Limit-Quota-Status (Enterprise-Grade Metering).

Wir testen drei Pfade:
1. ``quota_status_for(pct)`` — reine Funktion, Stufen-Mapping.
2. ``_next_utc_midnight_iso`` — Reset-Berechnung.
3. ``build_usage_headers`` — End-to-End-Pfad mit KeyConfig + DailyUsage.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.token_metering import (
    QUOTA_CRITICAL_PCT,
    QUOTA_WARN_PCT,
    DailyUsage,
    KeyConfig,
    TokenMeteringService,
    _next_utc_midnight_iso,
    quota_status_for,
)


class TestQuotaStatusFor:
    """Stufen-Mapping: ok / warn / critical / exceeded."""

    @pytest.mark.parametrize("pct", [0, 1, 50, 79])
    def test_ok_below_warn(self, pct: int) -> None:
        assert quota_status_for(pct) == "ok"

    @pytest.mark.parametrize("pct", [80, 85, 94])
    def test_warn_in_range(self, pct: int) -> None:
        assert quota_status_for(pct) == "warn"

    @pytest.mark.parametrize("pct", [95, 96, 99])
    def test_critical_in_range(self, pct: int) -> None:
        assert quota_status_for(pct) == "critical"

    @pytest.mark.parametrize("pct", [100, 101, 200])
    def test_exceeded(self, pct: int) -> None:
        assert quota_status_for(pct) == "exceeded"

    def test_warn_threshold_constant(self) -> None:
        # Schutz: wenn jemand QUOTA_WARN_PCT senkt/anhebt, muessen die
        # Tests-Konstanten mitziehen — sonst lautloser Drift.
        assert QUOTA_WARN_PCT == 80
        assert QUOTA_CRITICAL_PCT == 95


class TestNextUtcMidnightIso:
    """Reset-Berechnung: immer naechste UTC-Mitternacht."""

    def test_just_after_midnight(self) -> None:
        now = datetime(2026, 4, 26, 0, 0, 1, tzinfo=UTC)
        assert _next_utc_midnight_iso(now) == "2026-04-27T00:00:00Z"

    def test_just_before_midnight(self) -> None:
        now = datetime(2026, 4, 26, 23, 59, 59, tzinfo=UTC)
        assert _next_utc_midnight_iso(now) == "2026-04-27T00:00:00Z"

    def test_noon(self) -> None:
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
        assert _next_utc_midnight_iso(now) == "2026-04-27T00:00:00Z"

    def test_format_uses_z_suffix(self) -> None:
        result = _next_utc_midnight_iso()
        assert result.endswith("Z")
        # Kein "+00:00" mehr drin
        assert "+00:00" not in result


class TestBuildUsageHeaders:
    """End-to-End: build_usage_headers liefert vollstaendige Quota-Header."""

    def _service(self) -> TokenMeteringService:
        # Kein Redis — wir testen nur die Header-Funktion.
        return TokenMeteringService(redis_client=None)

    def test_returns_empty_for_unmetered_key(self) -> None:
        svc = self._service()
        headers = svc.build_usage_headers(None, DailyUsage(total_tokens=100))
        assert headers == {}

    def test_ok_status_under_warn_threshold(self) -> None:
        svc = self._service()
        cfg = KeyConfig(tenant_id="t1", daily_token_limit=1000)
        usage = DailyUsage(total_tokens=500)
        headers = svc.build_usage_headers(cfg, usage)
        assert headers["X-Orkoprox-Tokens-Used-Today"] == "500"
        assert headers["X-Orkoprox-Token-Limit"] == "1000"
        assert headers["X-Orkoprox-Usage-Pct"] == "50"
        assert headers["X-Orkoprox-Quota-Status"] == "ok"
        assert headers["X-Orkoprox-Tenant-Id"] == "t1"
        assert headers["X-Orkoprox-Quota-Reset"].endswith("Z")
        # Cost-Headers default to 0.0000 when no cost_micro_usd is set.
        assert headers["X-Orkoprox-Cost-EUR-Today"] == "0.0000"
        assert headers["X-Orkoprox-Cost-USD-Today"] == "0.0000"

    def test_cost_headers_for_ovh_usage(self) -> None:
        """Cost-Tracking: USD-microcents werden in EUR/USD-Headers gerendert."""
        svc = self._service()
        cfg = KeyConfig(tenant_id="ovh-tenant", daily_token_limit=1_000_000)
        # 410.000 microcents = $0.41 USD = ~€0.3772 EUR (USD_TO_EUR=0.92)
        usage = DailyUsage(total_tokens=2_000_000, cost_micro_usd=410_000)
        headers = svc.build_usage_headers(cfg, usage)
        assert headers["X-Orkoprox-Cost-USD-Today"] == "0.4100"
        assert headers["X-Orkoprox-Cost-EUR-Today"] == "0.3772"

    def test_warn_status_at_80_percent(self) -> None:
        svc = self._service()
        cfg = KeyConfig(tenant_id="t1", daily_token_limit=1000)
        usage = DailyUsage(total_tokens=800)
        headers = svc.build_usage_headers(cfg, usage)
        assert headers["X-Orkoprox-Quota-Status"] == "warn"

    def test_critical_status_at_95_percent(self) -> None:
        svc = self._service()
        cfg = KeyConfig(tenant_id="t1", daily_token_limit=1000)
        usage = DailyUsage(total_tokens=950)
        headers = svc.build_usage_headers(cfg, usage)
        assert headers["X-Orkoprox-Quota-Status"] == "critical"

    def test_exceeded_status_at_100_percent(self) -> None:
        svc = self._service()
        cfg = KeyConfig(tenant_id="t1", daily_token_limit=1000)
        usage = DailyUsage(total_tokens=1000)
        headers = svc.build_usage_headers(cfg, usage)
        assert headers["X-Orkoprox-Quota-Status"] == "exceeded"

    def test_unlimited_key_stays_ok(self) -> None:
        svc = self._service()
        cfg = KeyConfig(tenant_id="t1", daily_token_limit=0)
        usage = DailyUsage(total_tokens=999_999)
        headers = svc.build_usage_headers(cfg, usage)
        # Limit 0 = unbegrenzt; pct ist 0 (Sonderfall im Code), Status bleibt ok.
        assert headers["X-Orkoprox-Quota-Status"] == "ok"
        assert headers["X-Orkoprox-Usage-Pct"] == "0"


class TestRecordUsageCost:
    """Cost-Metering muss auch bei Proxy-Alias-Modellen persistieren."""

    def test_record_usage_writes_cost_micro_usd_for_chat_alias(self) -> None:
        # Use the real in-memory store so we test the actual behaviour rather
        # than a backend-specific protocol mock.
        from app.storage import MemoryStore

        store = MemoryStore()
        svc = TokenMeteringService(store=store)
        usage = svc.record_usage(
            "test_key",
            prompt_tokens=1000,
            completion_tokens=500,
            model="chat",
            provider="ovh",
        )

        # A priced OVH chat call must persist a positive cost in microcents,
        # and it must land in BOTH the daily and monthly windows.
        assert usage.cost_micro_usd > 0
        daily = svc.get_daily_usage("test_key")
        monthly = svc.get_monthly_usage("test_key")
        assert daily.cost_micro_usd == usage.cost_micro_usd
        assert monthly.cost_micro_usd == usage.cost_micro_usd
        assert daily.total_tokens == monthly.total_tokens > 0
