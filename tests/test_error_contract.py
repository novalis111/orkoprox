from __future__ import annotations

from app.providers.base import ProviderError


def test_validation_error_contract(client):
    response = client.post(
        "/v1/chat/completions",
        headers={"x-api-key": "test-key"},
        json={"messages": [], "stream": False},
    )
    assert response.status_code == 422
    body = response.json()
    assert set(body.keys()) >= {"status", "code", "message", "request_id"}
    assert body["code"] == "validation_error"


def test_provider_error_contract_missing_provider_config(client, monkeypatch):
    async def _raise_provider_error(*args, **kwargs):
        raise ProviderError(
            "forced upstream failure",
            status_code=502,
            code="upstream_error",
            retryable=False,
        )

    monkeypatch.setattr("app.main.provider_registry.chat_completions", _raise_provider_error)

    response = client.post(
        "/v1/chat/completions",
        headers={"x-api-key": "test-key"},
        json={
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )

    assert response.status_code == 502
    body = response.json()
    assert set(body.keys()) >= {"status", "code", "message", "request_id"}
