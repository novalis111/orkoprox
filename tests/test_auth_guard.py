from __future__ import annotations

import importlib

import pytest


def test_auth_guard_rejects_missing_key(client):
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "mock/test-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "missing_api_key"


def test_auth_guard_accepts_valid_key(client):
    response = client.post(
        "/v1/chat/completions",
        headers={"x-api-key": "test-key"},
        json={
            "model": "mock/test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )

    assert response.status_code == 200


def test_auth_guard_accepts_valid_bearer_token(client):
    response = client.post(
        "/v1/chat/completions",
        headers={"authorization": "Bearer test-key"},
        json={
            "model": "mock/test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )

    assert response.status_code == 200


def test_production_rejects_short_proxy_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("PROXY_AUTH_REQUIRED", "true")
    monkeypatch.setenv("PROXY_API_KEYS", "tc_key")

    import app.config

    importlib.reload(app.config)

    with pytest.raises(ValueError, match="PROXY_API_KEYS contains keys shorter than 40 characters"):
        app.config.Settings()
