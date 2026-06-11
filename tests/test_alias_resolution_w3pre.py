"""Tests fuer Alias-Resolution (W3-Pre.4).

Verifiziert dass die neuen Media-Aliases (vision_x, image, voice, voice_hq)
im Router korrekt resolved werden, sowie OpenAI-Compat-Mapping (dall-e-*,
whisper-1) auf die richtigen Tier-Aliases umgeleitet wird.

Wichtig: Tests laden Settings mit `_env_file=None`, damit lokale `.env`-
Overrides (z.B. Dev-Override BASETEN-Vision) die Defaults nicht
ueberlagern. Wir testen die kanonischen `config.py`-Defaults.
"""

from __future__ import annotations

from app.config import Settings
from app.providers.router import (
    OPENAI_COMPAT_MODEL_ALIASES,
    ProviderRegistry,
)


def _settings() -> Settings:
    """Settings ohne .env-Override (kanonische config.py-Defaults)."""
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ─── _ALL_ALIASES Inventar ─────────────────────────────────────────────


def test_all_aliases_includes_new_media_aliases():
    """W3-Pre.4: vision_x, image, voice, voice_hq muessen registriert sein."""
    aliases = ProviderRegistry._ALL_ALIASES
    for new_alias in ("vision_x", "image", "voice", "voice_hq"):
        assert new_alias in aliases, f"Alias {new_alias!r} fehlt in _ALL_ALIASES"


def test_all_aliases_legacy_intact():
    """Tier- und Task-Aliases bleiben unveraendert."""
    aliases = ProviderRegistry._ALL_ALIASES
    expected = {
        "xhigh", "high", "medium", "low",
        "classify", "extract", "compose", "chat", "reason", "report",
        "ocr", "vision",
    }
    missing = expected - aliases
    assert not missing, f"Aliases verloren: {missing}"


# ─── Settings Default-Werte (kanonisch, ohne .env-Override) ────────────


def test_settings_defaults_are_ovh_for_media_aliases():
    s = _settings()
    assert "ovh/" in s.model_alias_vision, (
        f"Default vision sollte OVH sein, ist {s.model_alias_vision!r}"
    )
    assert "Qwen2.5-VL-72B" in s.model_alias_vision_x
    assert "stable-diffusion-xl" in s.model_alias_image
    assert "whisper-large-v3-turbo" in s.model_alias_voice
    assert s.model_alias_voice_hq.endswith("whisper-large-v3")


def test_settings_defaults_text_premium_unchanged():
    """xhigh/reason bleiben auf den OVH-Premium-Modellen."""
    s = _settings()
    assert "Meta-Llama-3_3-70B" in s.model_alias_xhigh
    assert "gpt-oss-120b" in s.model_alias_reason


# ─── OpenAI-Compat-Aliases ─────────────────────────────────────────────


def test_openai_compat_dalle_maps_to_image():
    """Alle DALL-E-Varianten muessen auf `image`-Alias gehen."""
    for k in ("dall-e-2", "dall-e-3", "gpt-image-1"):
        assert OPENAI_COMPAT_MODEL_ALIASES.get(k) == "image", (
            f"OpenAI-Compat {k!r} sollte auf 'image' mappen"
        )


def test_openai_compat_whisper_maps_to_voice():
    assert OPENAI_COMPAT_MODEL_ALIASES.get("whisper-1") == "voice"


def test_openai_compat_only_real_openai_models():
    """Only real OpenAI SDK default model names are mapped (no fake aliases)."""
    assert set(OPENAI_COMPAT_MODEL_ALIASES.keys()) == {
        "dall-e-2", "dall-e-3", "gpt-image-1", "whisper-1",
    }


# ─── Router-Resolution (End-to-End) ────────────────────────────────────


def test_resolve_vision_alias_to_ovh_default():
    """`vision` (raw) → ovh/Mistral-Small-3.2-24B (Default)."""
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("vision")
    assert decision.preferred_provider == "ovh"
    assert "Mistral-Small-3.2-24B" in decision.preferred_model


def test_resolve_vision_x_alias_to_qwen():
    """`vision_x` → ovh/Qwen2.5-VL-72B-Instruct (Premium)."""
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("vision_x")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "Qwen2.5-VL-72B-Instruct"


def test_resolve_image_alias_to_sdxl():
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("image")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "stable-diffusion-xl-base-v10"


def test_resolve_voice_alias_to_whisper_turbo():
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("voice")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "whisper-large-v3-turbo"


def test_resolve_voice_hq_alias_to_whisper_v3():
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("voice_hq")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "whisper-large-v3"


def test_resolve_dalle3_via_openai_compat_to_sdxl():
    """OpenAI-Konsumenten: `dall-e-3` → image-Alias → SDXL auf OVH."""
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("dall-e-3")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "stable-diffusion-xl-base-v10"


def test_resolve_xhigh_unchanged_to_llama_70b():
    """Regression-Check: xhigh-Routing nicht versehentlich kaputt."""
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("xhigh")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "Meta-Llama-3_3-70B-Instruct"


def test_resolve_reason_unchanged_to_gpt_oss_120b():
    s = _settings()
    registry = ProviderRegistry(settings=s)
    decision = registry._resolve_route_decision("reason")
    assert decision.preferred_provider == "ovh"
    assert decision.preferred_model == "gpt-oss-120b"


# ─── Route-Key (fuer Fallback-Chain-Routing) ───────────────────────────


def test_route_key_for_new_aliases():
    for alias in ("vision_x", "image", "voice", "voice_hq"):
        assert ProviderRegistry._route_key(alias) == alias


def test_route_key_for_dalle_maps_via_openai_compat():
    """dall-e-3 ist nicht in _ALL_ALIASES, aber resolved via
    OPENAI_COMPAT_MODEL_ALIASES auf "image"."""
    # Direkt _route_key gibt "default" zurueck — Resolution erfolgt erst in
    # _resolve_route_decision via OPENAI_COMPAT_MODEL_ALIASES.
    assert ProviderRegistry._route_key("dall-e-3") == "default"
