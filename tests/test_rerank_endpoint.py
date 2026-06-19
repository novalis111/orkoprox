"""Contract tests for the /v1/rerank endpoint.

Reranking runs via the TEI sidecar (`reranker` provider). There are
NO real network calls in these tests — httpx is mocked.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


class _FakeResponse:
    """Minimaler httpx.Response-Ersatz fuer die TEI-Rerank-Antwort."""

    def __init__(self, status_code: int, json_body: Any) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.content = b"{}"

    def json(self) -> Any:
        return self._json_body


class _FakeAsyncClient:
    """Mockt httpx.AsyncClient — gibt eine vorgegebene Antwort zurueck.

    ``behaviour`` steuert den Fehlerfall:
      - "ok": liefert ``json_body``
      - "timeout": wirft httpx.TimeoutException
      - "transport": wirft httpx.ConnectError (Sidecar down)
      - "http_error": liefert status_code 500
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.behaviour: str = _FakeAsyncClient.behaviour
        self.json_body: Any = _FakeAsyncClient.json_body

    behaviour: str = "ok"
    json_body: Any = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        if self.behaviour == "timeout":
            raise httpx.TimeoutException("mock timeout")
        if self.behaviour == "transport":
            raise httpx.ConnectError("mock connection refused")
        if self.behaviour == "http_error":
            return _FakeResponse(500, {})
        return _FakeResponse(200, self.json_body)


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch,
    behaviour: str,
    json_body: Any,
) -> None:
    """Patcht httpx.AsyncClient im reranker-Provider-Modul.

    Aktiviert zugleich ``reranker_enabled`` auf den Settings, die der Router
    konsultiert — der Sidecar ist seit 2026-06 per Default AUS (2.3 GB opt-in),
    aber diese Tests prüfen ja gerade den AKTIVEN Rerank-Pfad."""
    import app.main as main_mod
    import app.providers.reranker as reranker_mod

    _FakeAsyncClient.behaviour = behaviour
    _FakeAsyncClient.json_body = json_body
    monkeypatch.setattr(reranker_mod.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        main_mod.provider_registry.settings, "reranker_enabled", True, raising=False
    )


# ─── Disabled-by-default guard ───────────────────────────────────────────────


def test_rerank_disabled_by_default_returns_503(client, monkeypatch):
    """Der Reranker-Sidecar ist seit 2026-06 per Default AUS (2.3 GB opt-in).
    Ohne RERANKER_ENABLED=true muss /v1/rerank sauber 503 liefern statt einen
    nie gestarteten Sidecar anzuwählen."""
    import app.main as main_mod

    monkeypatch.setattr(
        main_mod.provider_registry.settings, "reranker_enabled", False, raising=False
    )
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={"model": "rerank", "query": "q", "documents": ["a", "b"]},
    )
    assert response.status_code == 503


# ─── Auth-Guard ────────────────────────────────────────────────────────────


def test_rerank_requires_auth(client):
    response = client.post(
        "/v1/rerank",
        json={
            "model": "rerank",
            "query": "what is python",
            "documents": ["python is a snake", "python is a language"],
        },
    )
    assert response.status_code in (401, 403)


# ─── Schema-Validation ─────────────────────────────────────────────────────


def test_rerank_rejects_empty_documents(client):
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={"model": "rerank", "query": "q", "documents": []},
    )
    assert response.status_code == 422


def test_rerank_rejects_empty_query(client):
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={"model": "rerank", "query": "  ", "documents": ["a"]},
    )
    assert response.status_code == 422


def test_rerank_rejects_missing_model(client):
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={"query": "q", "documents": ["a"]},
    )
    assert response.status_code == 422


def test_rerank_rejects_invalid_top_n(client):
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={"model": "rerank", "query": "q", "documents": ["a"], "top_n": 0},
    )
    assert response.status_code == 422


# ─── Happy Path / Contract ─────────────────────────────────────────────────


def test_rerank_returns_cohere_compatible_contract(client, monkeypatch):
    # TEI liefert [{"index", "score"}, ...] — bewusst unsortiert reingeben,
    # der Provider muss nach relevance_score absteigend sortieren.
    _install_fake_httpx(
        monkeypatch,
        "ok",
        [
            {"index": 0, "score": 0.12},
            {"index": 1, "score": 0.95},
            {"index": 2, "score": 0.40},
        ],
    )
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={
            "model": "rerank",
            "query": "what is python",
            "documents": [
                "a snake species",
                "a programming language",
                "a comedy group",
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "results" in body
    assert "model" in body
    results = body["results"]
    assert len(results) == 3
    # Absteigend nach relevance_score sortiert
    scores = [r["relevance_score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    assert results[0]["index"] == 1
    # Ohne return_documents kein document-Feld
    assert results[0].get("document") is None


def test_rerank_top_n_truncates(client, monkeypatch):
    _install_fake_httpx(
        monkeypatch,
        "ok",
        [
            {"index": 0, "score": 0.10},
            {"index": 1, "score": 0.90},
            {"index": 2, "score": 0.50},
        ],
    )
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={
            "model": "rerank",
            "query": "q",
            "documents": ["d0", "d1", "d2"],
            "top_n": 2,
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2
    assert results[0]["index"] == 1
    assert results[1]["index"] == 2


def test_rerank_return_documents_attaches_text(client, monkeypatch):
    _install_fake_httpx(
        monkeypatch,
        "ok",
        [{"index": 0, "score": 0.80}, {"index": 1, "score": 0.20}],
    )
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={
            "model": "rerank",
            "query": "q",
            "documents": ["first doc", "second doc"],
            "return_documents": True,
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["document"]["text"] == "first doc"
    assert results[1]["document"]["text"] == "second doc"


# ─── Sidecar-Fehlerfaelle ──────────────────────────────────────────────────


def test_rerank_sidecar_down_returns_clean_error(client, monkeypatch):
    _install_fake_httpx(monkeypatch, "transport", [])
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={"model": "rerank", "query": "q", "documents": ["a", "b"]},
    )
    assert response.status_code == 503
    body = response.json()
    assert body.get("code") == "reranker_unavailable"


def test_rerank_sidecar_timeout_returns_504(client, monkeypatch):
    _install_fake_httpx(monkeypatch, "timeout", [])
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={"model": "rerank", "query": "q", "documents": ["a", "b"]},
    )
    assert response.status_code == 504
    assert response.json().get("code") == "reranker_timeout"


def test_rerank_sidecar_http_error_returns_502(client, monkeypatch):
    _install_fake_httpx(monkeypatch, "http_error", [])
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={"model": "rerank", "query": "q", "documents": ["a", "b"]},
    )
    assert response.status_code == 502
    assert response.json().get("code") == "reranker_upstream_error"


def test_rerank_disabled_returns_503(client, monkeypatch):
    import app.main

    monkeypatch.setattr(app.main.provider_registry.settings, "reranker_enabled", False)
    response = client.post(
        "/v1/rerank",
        headers={"x-api-key": "test-key"},
        json={"model": "rerank", "query": "q", "documents": ["a"]},
    )
    assert response.status_code == 503
    assert response.json().get("code") == "reranker_disabled"
