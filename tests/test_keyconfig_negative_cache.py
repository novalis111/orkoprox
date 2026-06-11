"""Tests fuer Negative-Cache-TTL in TokenMeteringService.

Vorher: get_key_config() cached negative Lookups (Key existiert nicht in
Redis) PERMANENT in self._config_cache. Wenn ein anderer Prozess
(keyconfig_bootstrap.py) den Key in Redis schreibt, sieht der Webserver-
Prozess das nie — Container-Restart noetig.

Jetzt: separater Negative-Cache mit NEGATIVE_CACHE_TTL_S=30s. Nach Ablauf
wird Redis erneut gefragt — Cross-Process-Bootstrap wird in <30s sichtbar.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.token_metering import (
    NEGATIVE_CACHE_TTL_S,
    KeyConfig,
    TokenMeteringService,
)


class _FakeRedis:
    """Minimaler Fake mit dict-Backend. Reicht fuer Cache-Tests."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial or {})

    def get(self, key: str) -> Any:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def insert(self, key: str, value: str) -> None:
        """Test-Hilfe: simuliert externen Schreiber (z.B. keyconfig_bootstrap)."""
        self._store[key] = value

    def remove(self, key: str) -> None:
        self._store.pop(key, None)


def _make_config(tenant: str = "t1", daily: int = 1000) -> KeyConfig:
    return KeyConfig(tenant_id=tenant, daily_token_limit=daily)


# ─── Negative-Cache-TTL Verhalten ─────────────────────────────────────


def test_negative_cache_ttl_is_30_seconds():
    """Default-TTL ist 30s — Sweet-Spot zwischen Bot-Daempfung + Bootstrap-Sichtbarkeit."""
    assert NEGATIVE_CACHE_TTL_S == 30.0


def test_negative_lookup_cached_for_ttl_window(monkeypatch):
    """Innerhalb der TTL-Window wird der Negative-Cache-Eintrag genutzt — kein Redis-Hit."""
    fake = _FakeRedis()
    svc = TokenMeteringService(redis_client=fake)

    # Erster Lookup: Miss, schreibt in Negative-Cache
    assert svc.get_key_config("nope") is None
    assert "nope" in svc._negative_cache

    # Wir tracken Redis-Calls
    call_count = {"n": 0}
    original_get = fake.get

    def _counting_get(key: str) -> Any:
        call_count["n"] += 1
        return original_get(key)

    fake.get = _counting_get  # type: ignore[method-assign]

    # Zweiter Lookup innerhalb TTL: kein Redis-Hit, nur Cache-Lookup
    assert svc.get_key_config("nope") is None
    assert call_count["n"] == 0  # Redis NICHT befragt


def test_negative_cache_expires_after_ttl(monkeypatch):
    """Nach Ablauf der TTL wird Redis erneut befragt."""
    fake = _FakeRedis()
    svc = TokenMeteringService(redis_client=fake)

    # Erste Abfrage: Miss
    assert svc.get_key_config("future_key") is None
    assert "future_key" in svc._negative_cache

    # Monkey-patch monotonic — simuliere 31s in der Zukunft
    real_monotonic = time.monotonic
    fake_now = real_monotonic() + 31.0
    monkeypatch.setattr("app.token_metering.time.monotonic", lambda: fake_now)

    # Externer Prozess schreibt den Key (simuliert keyconfig_bootstrap)
    cfg = _make_config(tenant="future")
    fake.insert("metering:config:future_key", json.dumps(cfg.to_dict()))

    # Lookup nach Ablauf: TTL abgelaufen, Redis wird erneut befragt, Key gefunden
    result = svc.get_key_config("future_key")
    assert result is not None
    assert result.tenant_id == "future"

    # Negative-Cache-Eintrag wurde verworfen (durch positiven Hit invalidiert)
    assert "future_key" not in svc._negative_cache


def test_negative_cache_within_ttl_returns_none_even_if_redis_now_has_key(monkeypatch):
    """KRITISCHE Cache-Garantie: innerhalb TTL wird KEIN Redis-Hit gemacht.

    Wenn Bootstrap den Key schreibt waehrend Negative-Cache aktiv ist,
    sieht der Webserver das ERST nach Ablauf der TTL — by-design (Hot-Path-
    Daempfung, kein O(N)-Bot-Hammer auf Redis).
    """
    fake = _FakeRedis()
    svc = TokenMeteringService(redis_client=fake)

    # Negativ-Cache aktivieren
    assert svc.get_key_config("racing_key") is None

    # Externer Schreiber bringt den Key — innerhalb TTL
    cfg = _make_config(tenant="racing")
    fake.insert("metering:config:racing_key", json.dumps(cfg.to_dict()))

    # Lookup innerhalb TTL: gibt None zurueck (Cache greift)
    assert svc.get_key_config("racing_key") is None


def test_register_key_invalidates_negative_cache():
    """Wenn der Webserver-Prozess SELBST den Key schreibt, muss Negative-Cache
    sofort invalidiert werden — kein TTL-Wait noetig."""
    fake = _FakeRedis()
    svc = TokenMeteringService(redis_client=fake)

    # Negative-Cache aktivieren
    assert svc.get_key_config("self_written") is None
    assert "self_written" in svc._negative_cache

    # Webserver schreibt selbst
    cfg = _make_config(tenant="self")
    svc.register_key("self_written", cfg)

    # Negative-Cache invalidiert
    assert "self_written" not in svc._negative_cache

    # Naechster Lookup findet den Key sofort (aus Positive-Cache)
    result = svc.get_key_config("self_written")
    assert result is not None
    assert result.tenant_id == "self"


def test_revoke_key_clears_both_caches():
    """revoke_key() entfernt Eintrag aus Positive- UND Negative-Cache."""
    fake = _FakeRedis()
    svc = TokenMeteringService(redis_client=fake)

    cfg = _make_config(tenant="to_revoke")
    svc.register_key("revoke_key", cfg)
    assert "revoke_key" in svc._config_cache

    svc.revoke_key("revoke_key")
    assert "revoke_key" not in svc._config_cache
    assert "revoke_key" not in svc._negative_cache


def test_positive_cache_unaffected_by_negative_cache_ttl(monkeypatch):
    """Positive-Cache hat KEIN TTL — nur Negative-Cache."""
    fake = _FakeRedis()
    svc = TokenMeteringService(redis_client=fake)

    cfg = _make_config(tenant="t1")
    fake.insert("metering:config:active_key", json.dumps(cfg.to_dict()))

    # Lookup: Treffer, in Positive-Cache
    result1 = svc.get_key_config("active_key")
    assert result1 is not None

    # Monkey-patch monotonic — 60s in Zukunft (jenseits TTL)
    fake_now = time.monotonic() + 60.0
    monkeypatch.setattr("app.token_metering.time.monotonic", lambda: fake_now)

    # Externer Aenderer entfernt den Key
    fake.remove("metering:config:active_key")

    # Lookup: aus Positive-Cache, NICHT erneut Redis fragen
    # (Positive-Cache wird NUR via register_key/revoke_key invalidiert)
    result2 = svc.get_key_config("active_key")
    assert result2 is not None
    assert result2.tenant_id == "t1"
