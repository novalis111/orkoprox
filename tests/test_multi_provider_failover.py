"""Tests für Multi-Provider-Failover (OVH → konfigurierbarer Sekundär-Provider).

Owner-Direktive 2026-06-22 (KRITISCH): OVH-Outage darf nicht alle Tenants
lahmlegen. Ein OVH-Ausfall soll automatisch auf einen Sekundär-Provider +
-Model failover'n — pro Tier konfigurierbar (chat→Mistral-Small,
reason/xhigh→Mistral-Large als Default) — mit provider-korrekter Abrechnung.

Abgedeckt:
1. Config: ``fallback_model_alias_map`` (gesetzt + leerer Default).
2. ``_model_for_candidate``: Primär behält Modell, Cross-Provider nimmt das
   konfigurierte Fallback-Model (reason→Large, chat→Small).
3. ``_configured_fallback_model``: Prefix-Match, Prefix-Mismatch (kein Leak),
   prefix-loses Target, unkonfigurierte Route.
4. Kein Verhaltenswechsel ohne Config (Backward-Compat → Provider-Default).
5. End-to-End: echter Failover durch die Chat-Routing-Schleife — OVH down →
   Mistral übernimmt mit dem Large-Modell, Rückgabe (provider, model) korrekt.
6. Provider-spezifisches Pricing/Token-Metering beim Fallback (Mistral-Pricing
   für den Mistral-Call, NICHT OVH-Pricing).
7. Policy-TOML: ``fallback_reason`` → ``model_alias_fallback_reason``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderRequestContext,
)
from app.providers.router import PROVIDER_ALIASES, ProviderRegistry


# ── Fake-Provider ──────────────────────────────────────────────────────────


class _FailingProvider(BaseProvider):
    """Simuliert einen ausgefallenen Provider (z.B. OVH-Outage)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=True,
            supports_parallel_tool_calls=True,
            supports_response_format=True,
        )
        self.api_key = "x"  # nicht-leer, damit provider_ready() True liefert
        self.call_count = 0

    async def chat_completions(
        self, payload: dict[str, Any], ctx: ProviderRequestContext
    ) -> dict[str, Any]:
        self.call_count += 1
        # 5xx → echter Provider-Ausfall, retryable, triggert Failover + Cooldown.
        raise ProviderError(
            "upstream down",
            status_code=502,
            code="upstream_error",
            retryable=True,
            details={"provider_status": 503},
        )

    async def chat_completions_stream(self, payload, ctx):  # type: ignore[override]
        self.call_count += 1
        raise ProviderError("upstream down", status_code=502, retryable=True)
        yield b""  # pragma: no cover

    async def embeddings(self, payload, ctx):  # type: ignore[override]
        raise ProviderError("upstream down", status_code=502, retryable=True)


class _EchoProvider(BaseProvider):
    """Gesunder Provider: gibt das angefragte Modell im Response zurück."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=True,
            supports_parallel_tool_calls=True,
            supports_response_format=True,
        )
        self.api_key = "x"
        self.seen_models: list[str] = []

    async def chat_completions(
        self, payload: dict[str, Any], ctx: ProviderRequestContext
    ) -> dict[str, Any]:
        model = payload.get("model", "")
        self.seen_models.append(model)
        return {
            "id": "chatcmpl-echo",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"ok from {self.name}"},
                    "finish_reason": "stop",
                }
            ],
        }

    async def chat_completions_stream(self, payload, ctx):  # type: ignore[override]
        yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def embeddings(self, payload, ctx):  # type: ignore[override]
        return {"object": "list", "data": [], "model": payload.get("model", "")}


def _failover_settings(**overrides: Any) -> Settings:
    """Settings für das Owner-Failover-Szenario: OVH primär, mistral_lp Fallback."""
    return Settings(
        proxy_api_keys="",
        primary_provider="ovh",
        default_provider="ovh",
        fallback_providers_chat="ovh,mistral_lp",
        fallback_providers_reason="ovh,mistral_lp",
        fallback_providers_xhigh="ovh,mistral_lp",
        model_alias_fallback_chat="mistral_lp/mistral-small-latest",
        model_alias_fallback_reason="mistral_lp/mistral-large-latest",
        model_alias_fallback_xhigh="mistral_lp/mistral-large-latest",
        # Cooldown 0 → kein Cross-Test-Leak; jeder Test startet sauber.
        provider_cooldown_seconds=0,
        **overrides,
    )


def _registry_with(ovh: BaseProvider, mistral: BaseProvider, settings: Settings) -> ProviderRegistry:
    registry = ProviderRegistry(settings)
    # Provider direkt injizieren, damit kein echter HTTP-Call passiert.
    registry._providers["ovh"] = ovh
    registry._providers["mistral_lp"] = mistral
    from app.providers.router import _ProviderState

    registry._state.setdefault("ovh", _ProviderState())
    registry._state.setdefault("mistral_lp", _ProviderState())
    return registry


# ── 1. Config: fallback_model_alias_map ────────────────────────────────────


def test_fallback_model_alias_map_reads_configured_entries() -> None:
    s = _failover_settings()
    m = s.fallback_model_alias_map
    assert m["chat"] == "mistral_lp/mistral-small-latest"
    assert m["reason"] == "mistral_lp/mistral-large-latest"
    assert m["xhigh"] == "mistral_lp/mistral-large-latest"


def test_fallback_model_alias_map_empty_by_default() -> None:
    """Ohne Config: leeres Map → kein Verhaltenswechsel (Backward-Compat)."""
    assert Settings(proxy_api_keys="").fallback_model_alias_map == {}


# ── 2./3./4. _model_for_candidate + _configured_fallback_model ─────────────


def test_primary_provider_keeps_explicit_model() -> None:
    """Auf dem Primär-Provider bleibt das explizit angefragte Modell."""
    r = ProviderRegistry(_failover_settings())
    assert (
        r._model_for_candidate(
            preferred_provider="ovh",
            candidate_provider="ovh",
            preferred_model="gpt-oss-120b",
            route_key="reason",
        )
        == "gpt-oss-120b"
    )


def test_cross_provider_reason_uses_configured_large() -> None:
    """reason failover → mistral-large-latest (Owner-Wunsch)."""
    r = ProviderRegistry(_failover_settings())
    assert (
        r._model_for_candidate(
            preferred_provider="ovh",
            candidate_provider="mistral_lp",
            preferred_model="gpt-oss-120b",
            route_key="reason",
        )
        == "mistral-large-latest"
    )


def test_cross_provider_chat_uses_configured_small() -> None:
    """chat failover → mistral-small-latest (Owner-Wunsch)."""
    r = ProviderRegistry(_failover_settings())
    assert (
        r._model_for_candidate(
            preferred_provider="ovh",
            candidate_provider="mistral_lp",
            preferred_model="Mistral-Small-3.2-24B-Instruct-2506",
            route_key="chat",
        )
        == "mistral-small-latest"
    )


def test_cross_provider_unconfigured_route_falls_back_to_provider_default() -> None:
    """Route ohne Fallback-Mapping → bisheriges Verhalten (Provider-Default)."""
    r = ProviderRegistry(_failover_settings())
    result = r._model_for_candidate(
        preferred_provider="ovh",
        candidate_provider="mistral_lp",
        preferred_model="Qwen3.5-9B",
        route_key="long_context",  # in _failover_settings NICHT gemappt
    )
    assert result == "mistral-small-latest"  # = mistral_lp_default_model


def test_configured_fallback_model_prefix_mismatch_does_not_leak() -> None:
    """Ein mistral_lp-Target darf NICHT auf einen anderen Fallback-Provider lecken."""
    s = Settings(proxy_api_keys="", model_alias_fallback_reason="mistral_lp/mistral-large-latest")
    r = ProviderRegistry(s)
    # Candidate ist 'groq', Target zeigt auf mistral_lp → kein Match.
    assert r._configured_fallback_model("groq", "reason") == ""


def test_configured_fallback_model_prefixless_applies_to_any_secondary() -> None:
    """Prefix-loses Target greift für jeden Sekundär (Single-Secondary-Setup)."""
    s = Settings(proxy_api_keys="", model_alias_fallback_reason="mistral-large-latest")
    r = ProviderRegistry(s)
    assert r._configured_fallback_model("mistral_lp", "reason") == "mistral-large-latest"


def test_configured_fallback_model_unknown_route_returns_empty() -> None:
    r = ProviderRegistry(_failover_settings())
    assert r._configured_fallback_model("mistral_lp", "does_not_exist") == ""


# ── 5. End-to-End: echter Failover durch die Routing-Schleife ──────────────


@pytest.mark.asyncio
async def test_end_to_end_failover_reason_routes_to_mistral_large() -> None:
    """OVH down + reason-Request → mistral_lp übernimmt mit mistral-large-latest.

    Verifiziert den KERN: der Router gibt den TATSÄCHLICH genutzten
    (provider, model) zurück — Basis für die korrekte Abrechnung.
    """
    settings = _failover_settings()
    ovh = _FailingProvider("ovh")
    mistral = _EchoProvider("mistral_lp")
    registry = _registry_with(ovh, mistral, settings)
    ctx = ProviderRequestContext(request_id="t", forward_headers={})

    payload = {"model": "reason", "messages": [{"role": "user", "content": "hi"}]}
    provider_name, model, data, _route_debug = await registry.chat_completions(
        "reason", payload, ctx
    )

    assert ovh.call_count >= 1  # OVH wurde zuerst versucht
    assert provider_name == "mistral_lp"  # auf Mistral failover't
    assert model == "mistral-large-latest"  # mit dem konfigurierten Large-Model
    assert data["model"] == "mistral-large-latest"
    assert mistral.seen_models == ["mistral-large-latest"]


@pytest.mark.asyncio
async def test_end_to_end_failover_chat_routes_to_mistral_small() -> None:
    settings = _failover_settings()
    ovh = _FailingProvider("ovh")
    mistral = _EchoProvider("mistral_lp")
    registry = _registry_with(ovh, mistral, settings)
    ctx = ProviderRequestContext(request_id="t", forward_headers={})

    payload = {"model": "chat", "messages": [{"role": "user", "content": "hi"}]}
    provider_name, model, _data, _ = await registry.chat_completions("chat", payload, ctx)

    assert provider_name == "mistral_lp"
    assert model == "mistral-small-latest"


@pytest.mark.asyncio
async def test_primary_healthy_no_failover() -> None:
    """OVH gesund → kein Failover, OVH antwortet (Recovery-Semantik)."""
    settings = _failover_settings()
    ovh = _EchoProvider("ovh")
    mistral = _EchoProvider("mistral_lp")
    registry = _registry_with(ovh, mistral, settings)
    ctx = ProviderRequestContext(request_id="t", forward_headers={})

    payload = {"model": "reason", "messages": [{"role": "user", "content": "hi"}]}
    provider_name, model, _data, _ = await registry.chat_completions("reason", payload, ctx)

    assert provider_name == "ovh"
    assert model == "gpt-oss-120b"
    assert mistral.seen_models == []  # Sekundär nie berührt


# ── 6. Provider-spezifisches Pricing/Token-Metering beim Fallback ──────────


def test_billing_uses_mistral_pricing_for_failover_model() -> None:
    """Beim Mistral-Fallback muss Mistral-Pricing greifen, NICHT OVH.

    Spiegelt den main.py-Aufruf: record_usage(model=<resolved>, provider=<actual>).
    """
    from app.providers.mistral_lp_pricing import cost_usd as mistral_cost
    from app.providers.ovh_pricing import cost_usd as ovh_cost
    from app.token_metering import TokenMeteringService

    svc = TokenMeteringService()
    # Failover-Werte wie sie der Router zurückgibt:
    usage = svc.record_usage(
        "tenant-key",
        prompt_tokens=1000,
        completion_tokens=1000,
        model="mistral-large-latest",
        provider="mistral_lp",
    )

    expected_usd = mistral_cost("mistral-large-latest", 1000, 1000)
    assert expected_usd > 0
    # Microcents = ceil(usd * 1e6); muss der Mistral-Rechnung entsprechen.
    assert usage.cost_micro_usd == max(1, int(round(expected_usd * 1_000_000)))
    # Gegencheck: OVH-Pricing für dieses Modell wäre 0 (nicht in OVH-Map) —
    # ein versehentlicher OVH-Pfad würde also 0 abrechnen → klarer Unterschied.
    assert ovh_cost("mistral-large-latest", 1000, 1000) == 0.0
    # Token-Weighting ist das Mistral-Gewicht (Large = 19.4×), nicht OVH-1.0.
    assert usage.total_tokens == 38800  # (1000+1000) * 19.4


def test_billing_token_weight_differs_small_vs_large() -> None:
    """Small vs Large müssen unterschiedlich gewichtet werden (echte Kostenrealität)."""
    from app.token_metering import TokenMeteringService

    svc = TokenMeteringService()
    small = svc.record_usage(
        "k-small", prompt_tokens=1000, completion_tokens=1000,
        model="mistral-small-latest", provider="mistral_lp",
    )
    large = svc.record_usage(
        "k-large", prompt_tokens=1000, completion_tokens=1000,
        model="mistral-large-latest", provider="mistral_lp",
    )
    assert large.total_tokens > small.total_tokens
    assert large.cost_micro_usd > small.cost_micro_usd


# ── 7. Policy-TOML-Integration ─────────────────────────────────────────────


def test_policy_toml_maps_fallback_aliases() -> None:
    """`[aliases] fallback_reason = "..."` → settings.model_alias_fallback_reason."""
    from app.policy import Policy, apply_policy_to_settings

    settings = Settings(proxy_api_keys="")
    assert settings.fallback_model_alias_map == {}

    policy = Policy.from_dict(
        {
            "aliases": {
                "fallback_reason": "mistral_lp/mistral-large-latest",
                "fallback_chat": "mistral_lp/mistral-small-latest",
            }
        }
    )
    apply_policy_to_settings(policy, settings)

    assert settings.model_alias_fallback_reason == "mistral_lp/mistral-large-latest"
    assert settings.fallback_model_alias_map["reason"] == "mistral_lp/mistral-large-latest"
    assert settings.fallback_model_alias_map["chat"] == "mistral_lp/mistral-small-latest"


# ── PROVIDER_ALIASES-Hygiene: mistral_lp ist ein bekannter Provider ────────


def test_mistral_lp_is_known_provider_alias() -> None:
    assert PROVIDER_ALIASES.get("mistral_lp") == "mistral_lp"
    assert PROVIDER_ALIASES.get("mistral_la_plateforme") == "mistral_lp"
