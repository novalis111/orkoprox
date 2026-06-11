"""Tests for /v1/healthz and the OVH backend probing logic.

Only OVH remains as a backend; US provider code paths have been removed.
"""

from __future__ import annotations


import httpx
import pytest


# ─── Unit-Tests fuer healthz.probe_all_backends ─────────────────────────────


@pytest.mark.asyncio
async def test_probe_ovh_ok(monkeypatch):
    """OVH antwortet 200 -> overall ok."""
    from app import healthz
    from app.config import Settings

    settings = Settings(ovh_api_key="test-key", ovh_base_url="https://oai.example/v1")

    async def _fake_request(self, method, url, **kwargs):
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    result = await healthz.probe_all_backends(settings)
    assert result["status"] == "ok"
    names = {b["name"] for b in result["backends"]}
    assert "ovh" in names
    assert result["cache"] == "miss"


@pytest.mark.asyncio
async def test_probe_ovh_down_is_critical(monkeypatch):
    """OVH ist critical. Down -> overall critical."""
    from app import healthz
    from app.config import Settings

    settings = Settings(ovh_api_key="test-key", ovh_base_url="https://oai.example/v1")

    async def _fake_request(self, method, url, **kwargs):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    result = await healthz.probe_all_backends(settings)
    assert result["status"] == "critical"
    ovh = next(b for b in result["backends"] if b["name"] == "ovh")
    assert ovh["status"] == "unavailable"


@pytest.mark.asyncio
async def test_probe_ovh_auth_401(monkeypatch):
    """401 vom OVH-Backend -> status=error mit '401'."""
    from app import healthz
    from app.config import Settings

    settings = Settings(ovh_api_key="bad-key", ovh_base_url="https://oai.example/v1")

    async def _fake_request(self, method, url, **kwargs):
        return httpx.Response(401)

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    result = await healthz.probe_all_backends(settings)
    ovh = next(b for b in result["backends"] if b["name"] == "ovh")
    assert ovh["status"] == "error"
    assert "401" in ovh["detail"]


@pytest.mark.asyncio
async def test_probe_ohne_ovh_key_disabled(monkeypatch):
    """Wenn OVH_API_KEY leer -> status=disabled mit Hinweis-Detail."""
    from app import healthz
    from app.config import Settings

    settings = Settings(ovh_api_key="")

    async def _fake_request(self, method, url, **kwargs):
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    result = await healthz.probe_all_backends(settings)
    ovh = next(b for b in result["backends"] if b["name"] == "ovh")
    assert ovh["status"] == "disabled"
    assert "OVH_API_KEY" in ovh["detail"]


@pytest.mark.asyncio
async def test_probe_ovh_timeout(monkeypatch):
    """TimeoutException -> status=unavailable."""
    from app import healthz
    from app.config import Settings

    settings = Settings(ovh_api_key="test-key", ovh_base_url="https://oai.example/v1")

    async def _fake_request(self, method, url, **kwargs):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    result = await healthz.probe_all_backends(settings)
    ovh = next(b for b in result["backends"] if b["name"] == "ovh")
    assert ovh["status"] == "unavailable"
    assert "Timeout" in ovh["detail"]


# ─── P3-18 (BUNDLE-A): 5xx + Connection-Reset Backend-Down-Szenarien ──


@pytest.mark.asyncio
async def test_probe_ovh_5xx_is_critical_502(monkeypatch):
    """OVH liefert 502 -> backend-status=degraded, overall=critical (einziges critical-Backend)."""
    from app import healthz
    from app.config import Settings

    settings = Settings(ovh_api_key="test-key", ovh_base_url="https://oai.example/v1")

    async def _fake_request(self, method, url, **kwargs):
        return httpx.Response(502, text="Bad Gateway")

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    result = await healthz.probe_all_backends(settings)
    assert result["status"] == "critical"
    ovh = next(b for b in result["backends"] if b["name"] == "ovh")
    assert ovh["status"] == "degraded"
    assert "502" in ovh["detail"]


@pytest.mark.asyncio
async def test_probe_ovh_5xx_is_critical_503(monkeypatch):
    """OVH liefert 503 -> overall=critical, detail enthaelt 503."""
    from app import healthz
    from app.config import Settings

    settings = Settings(ovh_api_key="test-key", ovh_base_url="https://oai.example/v1")

    async def _fake_request(self, method, url, **kwargs):
        return httpx.Response(503, text="Service Unavailable")

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    result = await healthz.probe_all_backends(settings)
    assert result["status"] == "critical"
    ovh = next(b for b in result["backends"] if b["name"] == "ovh")
    assert ovh["status"] == "degraded"
    assert "503" in ovh["detail"]


@pytest.mark.asyncio
async def test_probe_ovh_5xx_is_critical_504(monkeypatch):
    """OVH liefert 504 (Gateway-Timeout vom OVH-LB) -> overall=critical."""
    from app import healthz
    from app.config import Settings

    settings = Settings(ovh_api_key="test-key", ovh_base_url="https://oai.example/v1")

    async def _fake_request(self, method, url, **kwargs):
        return httpx.Response(504, text="Gateway Timeout")

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    result = await healthz.probe_all_backends(settings)
    assert result["status"] == "critical"
    ovh = next(b for b in result["backends"] if b["name"] == "ovh")
    assert ovh["status"] == "degraded"
    assert "504" in ovh["detail"]


@pytest.mark.asyncio
async def test_probe_ovh_connection_reset(monkeypatch):
    """RemoteProtocolError (Connection mid-request abgebrochen) -> status=unavailable, overall=critical."""
    from app import healthz
    from app.config import Settings

    settings = Settings(ovh_api_key="test-key", ovh_base_url="https://oai.example/v1")

    async def _fake_request(self, method, url, **kwargs):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response")

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)

    result = await healthz.probe_all_backends(settings)
    assert result["status"] == "critical"
    ovh = next(b for b in result["backends"] if b["name"] == "ovh")
    assert ovh["status"] == "unavailable"
    assert "RemoteProtocolError" in ovh["detail"]


# ─── HTTP-Endpoint-Tests /v1/healthz ────────────────────────────────────────


def test_healthz_endpoint_200_wenn_proxy_lebt(client, monkeypatch):
    """GET /v1/healthz mit Auth liefert 200 + JSON mit status + backends[]."""
    from app import healthz as hz_module

    async def _fake_probe(settings):
        return {
            "status": "ok",
            "backends": [
                {"name": "ovh", "status": "ok", "http_status": 200},
            ],
            "cache": "miss",
        }

    async def _fake_get(settings, redis_client):
        return await _fake_probe(settings)

    monkeypatch.setattr(hz_module, "probe_all_backends", _fake_probe)
    monkeypatch.setattr(hz_module, "get_healthz", _fake_get)

    resp = client.get("/v1/healthz", headers={"x-api-key": "test-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert any(b["name"] == "ovh" for b in data["backends"])


def test_healthz_endpoint_503_bei_critical(client, monkeypatch):
    """Wenn probe_all_backends status=critical liefert -> HTTP 503."""
    from app import healthz as hz_module

    async def _fake_probe(settings):
        return {
            "status": "critical",
            "backends": [
                {"name": "ovh", "status": "unavailable"},
            ],
            "cache": "miss",
        }

    async def _fake_get(settings, redis_client):
        return await _fake_probe(settings)

    monkeypatch.setattr(hz_module, "probe_all_backends", _fake_probe)
    monkeypatch.setattr(hz_module, "get_healthz", _fake_get)

    resp = client.get("/v1/healthz", headers={"x-api-key": "test-key"})
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "critical"


def test_healthz_ohne_api_key_401(client):
    """Ohne X-API-Key -> 401 (gleiche Auth-Regel wie /v1/models)."""
    resp = client.get("/v1/healthz")
    assert resp.status_code == 401


def test_healthz_no_cache_bypassed(client, monkeypatch):
    """?no_cache=true -> cache-Feld ist 'bypass' (get_healthz nicht aufgerufen)."""
    from app import healthz as hz_module

    probe_calls = []
    get_calls = []

    async def _fake_probe(settings):
        probe_calls.append(1)
        return {"status": "ok", "backends": [], "cache": "miss"}

    async def _fake_get(settings, redis_client):
        get_calls.append(1)
        return {"status": "ok", "backends": [], "cache": "hit"}

    monkeypatch.setattr(hz_module, "probe_all_backends", _fake_probe)
    monkeypatch.setattr(hz_module, "get_healthz", _fake_get)

    resp = client.get("/v1/healthz?no_cache=true", headers={"x-api-key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["cache"] == "bypass"
    assert len(probe_calls) == 1
    assert len(get_calls) == 0


def test_healthz_setzt_backend_up_metric(client, monkeypatch):
    """Nach /v1/healthz sind die backend_up-Gauges gesetzt und in /metrics sichtbar."""
    from app import healthz as hz_module

    async def _fake_probe(settings):
        return {
            "status": "ok",
            "backends": [
                {"name": "ovh", "status": "ok"},
            ],
            "cache": "miss",
        }

    async def _fake_get(settings, redis_client):
        return await _fake_probe(settings)

    monkeypatch.setattr(hz_module, "probe_all_backends", _fake_probe)
    monkeypatch.setattr(hz_module, "get_healthz", _fake_get)

    client.get("/v1/healthz", headers={"x-api-key": "test-key"})

    metrics_resp = client.get("/metrics")
    body = metrics_resp.text
    assert 'llm_proxy_backend_up{backend="ovh"} 1' in body


# ─── Cache-Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_healthz_cache_hit(monkeypatch):
    """Wenn Redis einen gueltigen Cache-Eintrag hat, wird probe_all_backends NICHT aufgerufen."""
    from app import healthz
    from app.config import Settings

    settings = Settings()

    class FakeRedis:
        async def get(self, key):
            import json

            return json.dumps({"status": "ok", "backends": [], "cache": "miss"})

        async def setex(self, key, ttl, value):
            pass

    probe_calls = []

    async def _fake_probe(s):
        probe_calls.append(1)
        return {"status": "ok", "backends": [], "cache": "miss"}

    monkeypatch.setattr(healthz, "probe_all_backends", _fake_probe)

    result = await healthz.get_healthz(settings, FakeRedis())
    assert result["cache"] == "hit"
    assert len(probe_calls) == 0


@pytest.mark.asyncio
async def test_get_healthz_cache_miss_probed(monkeypatch):
    """Bei Cache-Miss wird probe_all_backends aufgerufen und das Ergebnis gespeichert."""
    from app import healthz
    from app.config import Settings

    settings = Settings()

    stored: dict[str, str] = {}

    class FakeRedis:
        async def get(self, key):
            return None  # miss

        async def setex(self, key, ttl, value):
            stored[key] = value

    probe_calls = []

    async def _fake_probe(s):
        probe_calls.append(1)
        return {"status": "ok", "backends": [], "cache": "miss"}

    monkeypatch.setattr(healthz, "probe_all_backends", _fake_probe)

    await healthz.get_healthz(settings, FakeRedis())
    assert len(probe_calls) == 1
    assert any("healthz" in k for k in stored)


@pytest.mark.asyncio
async def test_get_healthz_cache_fehler_fallback_auf_probe(monkeypatch):
    """Wenn Redis kaputt ist, faellt der Endpoint auf direktes Probing zurueck
    statt zu crashen."""
    from app import healthz
    from app.config import Settings

    settings = Settings()

    class BrokenRedis:
        async def get(self, key):
            raise RuntimeError("redis down")

        async def setex(self, key, ttl, value):
            raise RuntimeError("redis down")

    async def _fake_probe(s):
        return {"status": "ok", "backends": [], "cache": "miss"}

    monkeypatch.setattr(healthz, "probe_all_backends", _fake_probe)

    result = await healthz.get_healthz(settings, BrokenRedis())
    assert result["status"] == "ok"
