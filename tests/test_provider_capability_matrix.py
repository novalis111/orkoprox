from __future__ import annotations

import pytest

from app.providers.capability_matrix import (
    DEFAULT_PROVIDER_CAPABILITY_MATRIX,
    parse_capability_overrides,
)


def test_default_capability_matrix_includes_eu_providers() -> None:
    """EU-only provider set: OVH (default) + Mistral-LP (premium reports) + stub.

    Mistral-LP is allowed as a premium provider (EU-DC Paris, GDPR-compliant,
    production tier without training opt-in). Consumers address it via
    `report_premium` / `report_structure` aliases, NOT as the default provider.
    """
    assert set(DEFAULT_PROVIDER_CAPABILITY_MATRIX.keys()) == {
        "ovh",
        "mistral_lp",
        "stub",
    }


def test_stub_provider_is_not_implemented() -> None:
    stub = DEFAULT_PROVIDER_CAPABILITY_MATRIX["stub"]
    assert stub.verification_level == "not_implemented"
    assert stub.capabilities.supports_stream is False
    assert stub.capabilities.supports_tools is False


def test_ovh_provider_capability_matrix() -> None:
    """OVH AI Endpoints muss alle Capabilities haben."""
    ovh = DEFAULT_PROVIDER_CAPABILITY_MATRIX["ovh"]
    assert ovh.verification_level == "live_verified"
    assert ovh.capabilities.supports_stream is True
    assert ovh.capabilities.supports_tools is True
    assert ovh.capabilities.supports_parallel_tool_calls is True
    assert ovh.capabilities.supports_response_format is True
    assert ovh.capabilities.supports_vision is True


def test_ovh_pricing_module_has_all_known_models() -> None:
    """Pricing-Map muss alle Default-Modelle aus config.py kennen, sonst
    werden Token-Counter mit cost=0 fuer Live-Calls geschrieben."""
    from app.providers.ovh_pricing import OVH_MODEL_PRICING_USD

    expected = {
        "Mistral-7B-Instruct-v0.3",
        "Mistral-Small-3.2-24B-Instruct-2506",
        "Meta-Llama-3_3-70B-Instruct",
        "Qwen3-Coder-30B-A3B-Instruct",
        "gpt-oss-20b",
        "gpt-oss-120b",
        "bge-multilingual-gemma2",
    }
    missing = expected - set(OVH_MODEL_PRICING_USD.keys())
    assert not missing, f"OVH_MODEL_PRICING_USD fehlt: {missing}"


def test_ovh_pricing_cost_calculation() -> None:
    """Smoke: Kostenberechnung liefert plausible USD/EUR-Werte."""
    from app.providers.ovh_pricing import cost_eur, cost_usd

    usd = cost_usd("Mistral-Small-3.2-24B-Instruct-2506", 1_000_000, 1_000_000)
    assert 0.40 < usd < 0.42
    eur = cost_eur("Mistral-Small-3.2-24B-Instruct-2506", 1_000_000, 1_000_000)
    assert 0.36 < eur < 0.38

    assert cost_usd("Unknown-Model", 1000, 1000) == 0.0


def test_parse_capability_overrides_reads_json_map() -> None:
    parsed = parse_capability_overrides(
        '{"ovh":{"supports_parallel_tool_calls":false},"stub":{"supports_response_format":false}}'
    )

    assert parsed["ovh"].supports_parallel_tool_calls is False
    assert parsed["ovh"].supports_stream is True
    assert parsed["stub"].supports_response_format is False


def test_parse_capability_overrides_rejects_invalid_shape() -> None:
    with pytest.raises(ValueError):
        parse_capability_overrides('["not-an-object"]')
