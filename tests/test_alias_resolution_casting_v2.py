"""Tests for alias resolution — casting aliases.

Verifies that the casting aliases (reason_lite, long_context, reason_mid)
are correctly resolved by the router.

These finer reasoning tiers sit between the chat and reason/xhigh tiers.
Mapping (authoritative source: app/config.py):
- reason_lite   = ovh/gpt-oss-20b   (small reasoning model, 131k context)
- long_context  = ovh/Qwen3.5-9B    (262k context)
- reason_mid    = ovh/Qwen3-32B     (mid-tier reasoner, 32k context)
"""

from __future__ import annotations

from app.config import Settings
from app.providers.router import ProviderRegistry


def _settings() -> Settings:
    """Settings ohne .env-Override (kanonische config.py-Defaults)."""
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ─── _ALL_ALIASES Inventar ─────────────────────────────────────────────


def test_all_aliases_includes_casting_v2_aliases():
    """reason_lite, long_context, reason_mid must be registered in _ALL_ALIASES."""
    aliases = ProviderRegistry._ALL_ALIASES
    for new_alias in ("reason_lite", "long_context", "reason_mid"):
        assert new_alias in aliases, f"Alias {new_alias!r} fehlt in _ALL_ALIASES"


def test_all_aliases_legacy_intact_after_casting_v2():
    """Tier-, Task- und Media-Aliases bleiben unveraendert."""
    aliases = ProviderRegistry._ALL_ALIASES
    expected = {
        "xhigh", "high", "medium", "low",
        "classify", "extract", "compose", "chat", "reason", "report",
        "ocr", "vision", "vision_x",
        "image", "voice", "voice_hq",
    }
    missing = expected - aliases
    assert not missing, f"Aliases verloren: {missing}"


# ─── Settings Default-Werte (kanonisch, ohne .env-Override) ────────────


def test_settings_defaults_for_casting_v2_aliases():
    """Mapping in app/config.py muss exakt sein, sonst Drift mit ovh_pricing.py."""
    s = _settings()
    assert s.model_alias_reason_lite == "ovh/gpt-oss-20b"
    assert s.model_alias_long_context == "ovh/Qwen3.5-9B"
    assert s.model_alias_reason_mid == "ovh/Qwen3-32B"


def test_fallback_providers_for_casting_v2_default_to_ovh():
    """The three reasoning-tier aliases default their fallback to ovh."""
    s = _settings()
    assert s.fallback_providers_reason_lite == "ovh"
    assert s.fallback_providers_long_context == "ovh"
    assert s.fallback_providers_reason_mid == "ovh"


def test_fallback_provider_list_for_route_casting_v2():
    """Route-spezifische Fallback-Chain liefert ['ovh'] fuer alle drei neuen."""
    s = _settings()
    for route in ("reason_lite", "long_context", "reason_mid"):
        chain = s.fallback_provider_list_for_route(route)
        assert chain == ["ovh"], (
            f"Route {route!r} sollte ['ovh'] liefern, ist {chain!r}"
        )


# ─── Router-Resolution (End-to-End) ────────────────────────────────────


def test_resolve_reason_lite_to_gpt_oss_20b():
    """`reason_lite` → ovh/gpt-oss-20b (kleines Reasoning-Modell)."""
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("reason_lite")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "gpt-oss-20b"


def test_resolve_long_context_to_qwen35_9b():
    """`long_context` → ovh/Qwen3.5-9B (262k Kontext)."""
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("long_context")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "Qwen3.5-9B"


def test_resolve_reason_mid_to_qwen3_32b():
    """`reason_mid` → ovh/Qwen3-32B (Mid-Tier-Reasoner)."""
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("reason_mid")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "Qwen3-32B"


def test_resolve_reason_unchanged_after_casting_v2():
    """Regression-Check: bestehender `reason` (gpt-oss-120b) nicht versehentlich umgeleitet."""
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("reason")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "gpt-oss-120b"


# ─── Route-Key (fuer Fallback-Chain-Routing) ───────────────────────────


def test_route_key_for_casting_v2_aliases():
    for alias in ("reason_lite", "long_context", "reason_mid"):
        assert ProviderRegistry._route_key(alias) == alias
