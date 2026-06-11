"""Vision routing tests.

- Capability filter detects image_url payloads (vision=True)
- Providers without supports_vision are skipped (e.g. text-only Llama)
- Model-level filter blocks e.g. a text-only model + image (not a vision model)
- Mistral-Small-3.2 + Qwen2.5-VL as real vision providers
- Cooldown differentiation: ocr scope = 10s, HTTP 429 = 5 min
"""

from __future__ import annotations



from app.config import Settings
from app.providers.base import (
    ProviderCapabilities,
    ProviderError,
)
from app.providers.capability_matrix import (
    DEFAULT_PROVIDER_CAPABILITY_MATRIX,
    MODEL_VISION_HINTS,
)
from app.providers.router import ProviderRegistry, _RequestCapabilities


# ─────────────────────────────────────────────────────────────────────────────
# Capability-Matrix
# ─────────────────────────────────────────────────────────────────────────────


def test_request_capabilities_text_only_payload_no_vision() -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "Hallo, was ist 2+2?"},
        ],
    }
    caps = ProviderRegistry._request_capabilities(payload, stream=False)
    assert caps.vision is False


def test_request_capabilities_image_url_payload_sets_vision() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Was siehst du?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc"},
                    },
                ],
            }
        ],
    }
    caps = ProviderRegistry._request_capabilities(payload, stream=False)
    assert caps.vision is True


def test_request_capabilities_input_image_alternative_format() -> None:
    """Neuere OpenAI-Konvention: type='input_image' statt 'image_url'."""
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "data:image/png;base64,xyz"},
                ],
            }
        ],
    }
    caps = ProviderRegistry._request_capabilities(payload, stream=False)
    assert caps.vision is True


def test_request_capabilities_handles_malformed_payload_gracefully() -> None:
    # Kein crash bei merkwuerdigen Strukturen
    assert ProviderRegistry._request_capabilities({}, stream=False).vision is False
    assert ProviderRegistry._request_capabilities(
        {"messages": "not-a-list"}, stream=False
    ).vision is False
    assert ProviderRegistry._request_capabilities(
        {"messages": [{"role": "user", "content": None}]}, stream=False
    ).vision is False


def test_request_capabilities_as_list_includes_vision() -> None:
    caps = _RequestCapabilities(vision=True, tools=True)
    assert "vision" in caps.as_list()
    assert "tools" in caps.as_list()


# ─────────────────────────────────────────────────────────────────────────────
# Capability-Filter im Router (Provider-Level)
# ─────────────────────────────────────────────────────────────────────────────


def _vision_payload(prompt: str = "Lies die Rechnung") -> dict:
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                    },
                ],
            }
        ],
    }


def test_ocr_fallback_chain_parser_handles_legacy_strings() -> None:
    """``fallback_provider_list_for_route`` is a pure comma-splitter — it also
    works with arbitrary ENV strings without actually starting those providers.

    The production allowlist is enforced in boot validation
    (test_provider_allowlist), not in the splitter. The splitter stays
    provider-agnostic so an ENV value can't crash boot before the validator
    has a chance to report it.
    """
    settings = Settings(
        proxy_api_keys="",
        fallback_providers="",  # kein globaler Fallback dazu
        fallback_providers_default="",
        fallback_providers_ocr="legacy_a,legacy_b",  # neutrale Test-Strings
    )
    chain = settings.fallback_provider_list_for_route("ocr")
    assert chain[0] == "legacy_a"
    assert "legacy_b" in chain
    assert "ollama" not in chain


def test_vision_fallback_chain_parser_handles_legacy_strings() -> None:
    settings = Settings(
        proxy_api_keys="",
        fallback_providers="",
        fallback_providers_default="",
        fallback_providers_vision="legacy_a,legacy_b",
    )
    chain = settings.fallback_provider_list_for_route("vision")
    assert "legacy_a" in chain
    assert "legacy_b" in chain
    assert "ollama" not in chain


def test_code_default_for_ocr_chain_ovh_only_2026_05_04() -> None:
    """Direkter Test des Code-Defaults (Settings-Klasse), nicht der Live-.env.

    OVH-Switch (2026-05-04) + EU-AI-Act-Compliance + Ollama-zu-langsam:
    Default-Chain ist 'ovh' allein. Groq + Baseten + Together sind
    US-Provider und damit AI-Act-Risiko. Ollama auf prod CPU-only > 30s.
    """
    field = Settings.model_fields["fallback_providers_ocr"]
    default = field.default
    assert default == "ovh"


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown-Differenzierung
# ─────────────────────────────────────────────────────────────────────────────


def test_cooldown_for_ocr_scope_uses_short_default() -> None:
    settings = Settings(
        proxy_api_keys="",
        provider_cooldown_seconds=30,
        provider_cooldown_seconds_ocr=10,
        provider_cooldown_seconds_rate_limited=300,
    )
    registry = ProviderRegistry(settings)
    # Normaler Provider-Fehler im OCR-Scope: 10s
    exc = ProviderError("upstream error", status_code=502)
    assert registry._cooldown_seconds_for("ocr", exc) == 10
    assert registry._cooldown_seconds_for("vision", exc) == 10


def test_cooldown_for_chat_scope_uses_global_default() -> None:
    settings = Settings(
        proxy_api_keys="",
        provider_cooldown_seconds=30,
        provider_cooldown_seconds_ocr=10,
    )
    registry = ProviderRegistry(settings)
    exc = ProviderError("upstream error", status_code=502)
    assert registry._cooldown_seconds_for("chat", exc) == 30
    assert registry._cooldown_seconds_for("", exc) == 30


def test_cooldown_for_429_uses_long_rate_limit_default() -> None:
    """429 → 5 min Cooldown, egal welcher Scope (Free-Tier-Schutz)."""
    settings = Settings(
        proxy_api_keys="",
        provider_cooldown_seconds=30,
        provider_cooldown_seconds_ocr=10,
        provider_cooldown_seconds_rate_limited=300,
    )
    registry = ProviderRegistry(settings)
    rl_exc = ProviderError("rate limit hit", status_code=429)
    assert registry._cooldown_seconds_for("ocr", rl_exc) == 300
    assert registry._cooldown_seconds_for("chat", rl_exc) == 300
    assert registry._cooldown_seconds_for("", rl_exc) == 300


def test_cooldown_for_no_exception_uses_scope_default() -> None:
    settings = Settings(
        proxy_api_keys="",
        provider_cooldown_seconds=30,
        provider_cooldown_seconds_ocr=10,
    )
    registry = ProviderRegistry(settings)
    assert registry._cooldown_seconds_for("ocr", None) == 10
    assert registry._cooldown_seconds_for("chat", None) == 30


# ─────────────────────────────────────────────────────────────────────────────
# MODEL_VISION_HINTS Datenintegritaet
# ─────────────────────────────────────────────────────────────────────────────


def test_model_vision_hints_present_for_documented_providers() -> None:
    """Jeder Provider mit supports_vision=True sollte Modell-Hints haben."""
    for provider_name, profile in DEFAULT_PROVIDER_CAPABILITY_MATRIX.items():
        if profile.capabilities.supports_vision:
            assert provider_name in MODEL_VISION_HINTS, (
                f"Provider {provider_name} hat supports_vision=True, "
                f"aber keine MODEL_VISION_HINTS Eintraege"
            )


def test_default_capabilities_supports_vision_false() -> None:
    """Sicherheit: ProviderCapabilities() default ist Vision=False."""
    caps = ProviderCapabilities()
    assert caps.supports_vision is False
