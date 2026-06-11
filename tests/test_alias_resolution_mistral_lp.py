"""Tests for Mistral La Plateforme provider integration.

Mistral-LP is a premium provider for high-quality structured reports.
NOT the default — request via explicit tier aliases:

- `report_premium`   = mistral_lp/mistral-large-latest (text stage, T 0.7)
- `report_structure` = mistral_lp/mistral-small-latest (JSON stage, T 0.2)

Both have an OVH fallback (`xhigh` / `chat`) so a Mistral-LP outage
degrades brand-voice quality without killing the report entirely.

Pricing/weighting: app/providers/mistral_lp_pricing.py.
"""

from __future__ import annotations

from app.config import Settings
from app.providers.capability_matrix import (
    DEFAULT_PROVIDER_CAPABILITY_MATRIX,
)
from app.providers.mistral_lp_pricing import (
    MISTRAL_LP_PRICING_USD,
    MISTRAL_LP_TOKEN_WEIGHT,
    billable_tokens,
    cost_usd,
    is_priced,
    resolve_pricing_model,
)
from app.providers.router import (
    PROVIDER_ALIASES,
    ProviderRegistry,
)


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ─── Alias-Inventar ────────────────────────────────────────────────────


def test_all_aliases_includes_report_premium_and_structure():
    aliases = ProviderRegistry._ALL_ALIASES
    assert "report_premium" in aliases
    assert "report_structure" in aliases


def test_provider_alias_table_includes_mistral_lp():
    assert PROVIDER_ALIASES["mistral_lp"] == "mistral_lp"
    assert PROVIDER_ALIASES["mistral_la_plateforme"] == "mistral_lp"


# ─── Settings Default-Werte ────────────────────────────────────────────


def test_settings_defaults_for_mistral_lp():
    s = _settings()
    assert s.mistral_lp_base_url == "https://api.mistral.ai/v1"
    assert s.mistral_lp_default_model == "mistral-small-latest"
    assert s.model_alias_report_premium == "mistral_lp/mistral-large-latest"
    assert s.model_alias_report_structure == "mistral_lp/mistral-small-latest"


def test_fallback_providers_default_to_ovh():
    """report_premium und report_structure fallen auf OVH zurück."""
    s = _settings()
    assert s.fallback_providers_report_premium == "ovh"
    assert s.fallback_providers_report_structure == "ovh"


def test_fallback_chain_for_report_routes():
    s = _settings()
    for route in ("report_premium", "report_structure"):
        chain = s.fallback_provider_list_for_route(route)
        assert chain == ["ovh"], f"Route {route!r} unexpected chain: {chain!r}"


# ─── Router-Resolution ─────────────────────────────────────────────────


def test_resolve_report_premium_to_mistral_large():
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("report_premium")
    assert decision.preferred_provider == "mistral_lp"
    assert decision.preferred_model == "mistral-large-latest"
    assert decision.route_key == "report_premium"


def test_resolve_report_structure_to_mistral_small():
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("report_structure")
    assert decision.preferred_provider == "mistral_lp"
    assert decision.preferred_model == "mistral-small-latest"
    assert decision.route_key == "report_structure"


def test_default_model_for_mistral_lp_provider():
    s = _settings()
    registry = ProviderRegistry(settings=s)
    assert registry._default_model_for_provider("mistral_lp") == "mistral-small-latest"
    # Embeddings + Vision NICHT auf Mistral-LP — geht über OVH
    assert registry._default_model_for_provider("mistral_lp", is_embedding=True) == ""
    assert registry._default_model_for_provider("mistral_lp", is_vision=True) == ""


# ─── Capability-Matrix ─────────────────────────────────────────────────


def test_capability_matrix_includes_mistral_lp():
    profile = DEFAULT_PROVIDER_CAPABILITY_MATRIX["mistral_lp"]
    assert profile.capabilities.supports_stream
    assert profile.capabilities.supports_tools
    # Mistral-LP unterstützt KEIN parallel_tool_calls (Stand 2026-05).
    assert not profile.capabilities.supports_parallel_tool_calls
    assert profile.capabilities.supports_response_format
    assert profile.capabilities.supports_vision  # via Pixtral


def test_vision_hints_only_pixtral_for_mistral_lp():
    """Mistral-Large und Mistral-Small sind text-only; nur Pixtral kann Bilder."""
    from app.providers.capability_matrix import model_supports_vision

    assert not model_supports_vision("mistral_lp", "mistral-large-latest")
    assert not model_supports_vision("mistral_lp", "mistral-small-latest")
    assert model_supports_vision("mistral_lp", "pixtral-large-latest")


# ─── Pricing + Weighting ───────────────────────────────────────────────


def test_pricing_map_has_premium_and_small_models():
    assert "mistral-large-latest" in MISTRAL_LP_PRICING_USD
    assert "mistral-small-latest" in MISTRAL_LP_PRICING_USD


def test_resolve_pricing_model_aliases():
    assert resolve_pricing_model("mistral-large") == "mistral-large-latest"
    assert resolve_pricing_model("Mistral-Large") == "mistral-large-latest"
    assert resolve_pricing_model("mistral-large-latest") == "mistral-large-latest"
    assert resolve_pricing_model("") == ""


def test_token_weight_large_is_premium_anchor():
    """Mistral-Large = 19.4× — bewusst Premium-Cost-Signal an Konsument."""
    assert MISTRAL_LP_TOKEN_WEIGHT["mistral-large-latest"] == 19.4
    assert MISTRAL_LP_TOKEN_WEIGHT["mistral-small-latest"] == 1.9


def test_billable_tokens_large():
    # 1000 prompt + 500 completion @ weight 19.4 → 29100 virtuelle Tokens
    result = billable_tokens("mistral-large-latest", prompt_tokens=1000, completion_tokens=500)
    assert result == 29100


def test_billable_tokens_small():
    # 1000 prompt + 500 completion @ weight 1.9 → 2850 virtuelle Tokens
    result = billable_tokens("mistral-small-latest", prompt_tokens=1000, completion_tokens=500)
    assert result == 2850


def test_billable_tokens_unknown_model_uses_premium_default():
    """Unbekannte Mistral-LP-Modelle bekommen weight=5 (kein Free-Lunch)."""
    result = billable_tokens("mistral-unknown", prompt_tokens=100, completion_tokens=100)
    assert result == 1000  # 200 * 5 = 1000


def test_cost_usd_large():
    """Mistral-Large $2.00/$6.00 per M → 1000 in + 500 out = $0.005."""
    usd = cost_usd("mistral-large-latest", prompt_tokens=1000, completion_tokens=500)
    assert abs(usd - (1000 * 0.000002 + 500 * 0.000006)) < 1e-9


def test_is_priced():
    assert is_priced("mistral-large-latest")
    assert is_priced("mistral-large")  # via Alias
    assert not is_priced("mistral-llama-3-70b")  # nicht Mistral-LP
    assert not is_priced("")
