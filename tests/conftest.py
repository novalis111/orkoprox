from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Tests use "mock" provider (deterministic fake responses).
    # Production StubProvider always 503s — it's a safety net, not a mock.
    monkeypatch.setenv("PRIMARY_PROVIDER", "mock")
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("FALLBACK_PROVIDERS", "mock")
    monkeypatch.setenv("PROXY_API_KEYS", "test-key")
    monkeypatch.setenv("METRICS_ENABLED", "true")

    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)

    # Register mock provider so the router can resolve "mock" prefix
    from tests.mock_provider import TestMockProvider

    from app.providers.router import PROVIDER_ALIASES

    PROVIDER_ALIASES["mock"] = "mock"
    app.main.provider_registry._providers["mock"] = TestMockProvider()

    with TestClient(app.main.app) as test_client:
        yield test_client

    for key in [
        "PRIMARY_PROVIDER",
        "DEFAULT_PROVIDER",
        "FALLBACK_PROVIDERS",
        "PROXY_API_KEYS",
        "METRICS_ENABLED",
    ]:
        os.environ.pop(key, None)
