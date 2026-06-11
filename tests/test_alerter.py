"""Tests fuer den Alerter — vor allem die fetch_healthz()-Fehlerklassen.

Der Alerter ist aeusserlich ein stand-alone Script, aber der fetch_healthz()-
Kern ist pure Logik und testbar.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest


@pytest.fixture()
def alerter_module(monkeypatch):
    """Lade scripts/alerter.py als Modul — im Script-Pfad, nicht im Package."""
    script_dir = Path(__file__).resolve().parent.parent / "scripts"
    monkeypatch.syspath_prepend(str(script_dir))

    # Env-Vars setzen, damit das Modul ohne Netzwerk importiert werden kann
    monkeypatch.setenv("PROXY_URL", "http://proxy-mock:8091")
    monkeypatch.setenv("REDIS_URL", "redis://redis-mock:6379/0")
    monkeypatch.setenv("PROXY_INTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "")

    if "alerter" in sys.modules:
        del sys.modules["alerter"]
    alerter = importlib.import_module("alerter")
    return alerter


def test_fetch_healthz_ok_parsed(alerter_module, monkeypatch):
    """HTTP 200 + JSON -> HealthzFetchResult(kind='ok', data=...)."""

    def _fake_get(url, headers=None, timeout=None):
        return httpx.Response(
            200,
            json={"status": "ok", "backends": [{"name": "baseten", "status": "ok"}]},
        )

    monkeypatch.setattr(alerter_module.httpx, "get", _fake_get)

    result = alerter_module.fetch_healthz()
    assert result.kind == "ok"
    assert result.data["status"] == "ok"


def test_fetch_healthz_503_critical_also_ok(alerter_module, monkeypatch):
    """HTTP 503 (Proxy sagt 'critical') ist fuer den Alerter trotzdem 'ok' — er
    hat die Info bekommen und kann daraus check_backends ableiten."""

    def _fake_get(url, headers=None, timeout=None):
        return httpx.Response(
            503,
            json={"status": "critical", "backends": []},
        )

    monkeypatch.setattr(alerter_module.httpx, "get", _fake_get)

    result = alerter_module.fetch_healthz()
    assert result.kind == "ok"
    assert result.data["status"] == "critical"


def test_fetch_healthz_401_ist_auth_failed_nicht_unreachable(alerter_module, monkeypatch):
    """HTTP 401 MUSS als 'auth_failed' klassifiziert werden — NICHT als 'unreachable'.

    Sonst gibt es falsche PROXY-DOWN-Alerts wenn nur der API-Key fehlt/rotiert wurde.
    """

    def _fake_get(url, headers=None, timeout=None):
        return httpx.Response(401)

    monkeypatch.setattr(alerter_module.httpx, "get", _fake_get)

    result = alerter_module.fetch_healthz()
    assert result.kind == "auth_failed"
    assert "401" in result.detail or "PROXY_API_KEYS" in result.detail


def test_fetch_healthz_404_ist_unexpected_status(alerter_module, monkeypatch):
    """HTTP 404 / 500 etc. -> kind='unexpected_status' (Proxy lebt aber antwortet komisch)."""

    def _fake_get(url, headers=None, timeout=None):
        return httpx.Response(404)

    monkeypatch.setattr(alerter_module.httpx, "get", _fake_get)

    result = alerter_module.fetch_healthz()
    assert result.kind == "unexpected_status"
    assert "404" in result.detail


def test_fetch_healthz_connect_error_ist_unreachable(alerter_module, monkeypatch):
    """ConnectError / TimeoutException -> kind='unreachable' (echter Down)."""

    def _fake_get(url, headers=None, timeout=None):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(alerter_module.httpx, "get", _fake_get)

    result = alerter_module.fetch_healthz()
    assert result.kind == "unreachable"
    assert "refused" in result.detail.lower()


def test_fetch_healthz_timeout_ist_unreachable(alerter_module, monkeypatch):
    def _fake_get(url, headers=None, timeout=None):
        raise httpx.TimeoutException("request timed out")

    monkeypatch.setattr(alerter_module.httpx, "get", _fake_get)

    result = alerter_module.fetch_healthz()
    assert result.kind == "unreachable"


def test_fetch_healthz_json_parse_error_ist_unexpected(alerter_module, monkeypatch):
    """Proxy antwortet mit 200 aber Muell -> unexpected_status, nicht ok."""

    def _fake_get(url, headers=None, timeout=None):
        # content-type passend, aber kein JSON
        return httpx.Response(200, content=b"<html>gateway</html>")

    monkeypatch.setattr(alerter_module.httpx, "get", _fake_get)

    result = alerter_module.fetch_healthz()
    assert result.kind == "unexpected_status"
