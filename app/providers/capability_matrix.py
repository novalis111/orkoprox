from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.providers.base import BaseProvider, ProviderCapabilities, ProviderRequestContext


@dataclass(frozen=True)
class ProviderCapabilityProfile:
    provider: str
    capabilities: ProviderCapabilities
    verification_level: str
    notes: str


DEFAULT_PROVIDER_CAPABILITY_MATRIX: dict[str, ProviderCapabilityProfile] = {
    "ovh": ProviderCapabilityProfile(
        provider="ovh",
        capabilities=ProviderCapabilities(
            supports_stream=True,
            supports_tools=True,
            supports_parallel_tool_calls=True,
            supports_response_format=True,
            supports_vision=True,
        ),
        verification_level="live_verified",
        notes="OVH AI Endpoints — OpenAI-Spec, EU-DC (DSGVO), Bearer-Token-Auth.",
    ),
    "mistral_lp": ProviderCapabilityProfile(
        provider="mistral_lp",
        capabilities=ProviderCapabilities(
            supports_stream=True,
            supports_tools=True,
            # Mistral-LP unterstützt KEIN parallel_tool_calls (Stand 2026-05).
            # Sequenziell ist OK.
            supports_parallel_tool_calls=False,
            # response_format={"type":"json_object"} ist supported (Stage B).
            supports_response_format=True,
            # Vision nur via Pixtral-Large (explizit, kein Default).
            supports_vision=True,
        ),
        verification_level="live_verified",
        notes=(
            "Mistral La Plateforme — OpenAI-Spec, EU-DC (Paris), Bearer-Token. "
            "Premium provider, production tier. No training opt-in on client data."
        ),
    ),
    "stub": ProviderCapabilityProfile(
        provider="stub",
        capabilities=ProviderCapabilities(
            supports_stream=False,
            supports_tools=False,
            supports_parallel_tool_calls=False,
            supports_response_format=False,
            supports_vision=False,
        ),
        verification_level="not_implemented",
        notes="Always returns 503. Production safety net — if reached, OVH has failed.",
    ),
}


MODEL_VISION_HINTS: dict[str, set[str]] = {
    "ovh": {
        "mistral-small-3.2-24b-instruct-2506",
        "qwen2.5-vl-72b-instruct",
        "qwen3.5-9b",
        "stable-diffusion-xl-base-v10",
    },
    "mistral_lp": {
        # Nur Pixtral-Large unterstützt Bilder. Mistral-Large/Small sind text-only.
        "pixtral-large-latest",
        "pixtral-12b-2409",
    },
}


def model_supports_vision(provider_name: str, model_name: str) -> bool:
    """True wenn das konkrete Modell laut MODEL_VISION_HINTS Vision unterstuetzt.

    Provider ohne Eintrag → False (konservativer Default). Empty model → False.
    """
    if not model_name:
        return False
    hints = MODEL_VISION_HINTS.get(provider_name)
    if not hints:
        return False
    needle = model_name.lower()
    return any(hint in needle for hint in hints)


def parse_capability_overrides(raw: str) -> dict[str, ProviderCapabilities]:
    value = (raw or "").strip()
    if not value:
        return {}
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("PROVIDER_CAPABILITY_OVERRIDES must decode to an object")
    parsed: dict[str, ProviderCapabilities] = {}
    for provider_name, config in decoded.items():
        if not isinstance(provider_name, str) or not provider_name.strip():
            raise ValueError("PROVIDER_CAPABILITY_OVERRIDES contains an invalid provider name")
        if not isinstance(config, dict):
            raise ValueError(f"PROVIDER_CAPABILITY_OVERRIDES[{provider_name!r}] must be an object")
        parsed[provider_name.strip()] = ProviderCapabilities(
            supports_stream=bool(config.get("supports_stream", True)),
            supports_tools=bool(config.get("supports_tools", True)),
            supports_parallel_tool_calls=bool(config.get("supports_parallel_tool_calls", True)),
            supports_response_format=bool(config.get("supports_response_format", True)),
            supports_vision=bool(config.get("supports_vision", False)),
        )
    return parsed


def resolve_provider_capability_profile(
    provider_name: str,
    provider: BaseProvider,
    overrides: dict[str, ProviderCapabilities] | None = None,
) -> ProviderCapabilityProfile:
    base_profile = DEFAULT_PROVIDER_CAPABILITY_MATRIX.get(
        provider_name,
        ProviderCapabilityProfile(
            provider=provider_name,
            capabilities=getattr(provider, "capabilities", ProviderCapabilities()),
            verification_level="declared",
            notes="Fallback profile derived from provider class defaults.",
        ),
    )
    override = (overrides or {}).get(provider_name)
    capabilities = override or base_profile.capabilities
    notes = base_profile.notes if override is None else f"{base_profile.notes} Runtime override active."
    return ProviderCapabilityProfile(
        provider=provider_name,
        capabilities=capabilities,
        verification_level=base_profile.verification_level,
        notes=notes,
    )


def serialize_capability_matrix(
    overrides: dict[str, ProviderCapabilities] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider_name in sorted(DEFAULT_PROVIDER_CAPABILITY_MATRIX):
        profile = resolve_provider_capability_profile(
            provider_name,
            provider=_MatrixProviderStub(DEFAULT_PROVIDER_CAPABILITY_MATRIX[provider_name].capabilities),
            overrides=overrides,
        )
        rows.append(
            {
                "provider": profile.provider,
                "supports_stream": profile.capabilities.supports_stream,
                "supports_tools": profile.capabilities.supports_tools,
                "supports_parallel_tool_calls": profile.capabilities.supports_parallel_tool_calls,
                "supports_response_format": profile.capabilities.supports_response_format,
                "supports_vision": profile.capabilities.supports_vision,
                "vision_models": sorted(MODEL_VISION_HINTS.get(provider_name, set())),
                "verification_level": profile.verification_level,
                "notes": profile.notes,
            }
        )
    return rows


class _MatrixProviderStub(BaseProvider):
    def __init__(self, capabilities: ProviderCapabilities):
        self.name = "matrix"
        self.capabilities = capabilities

    async def chat_completions(self, payload: dict[str, Any], ctx: ProviderRequestContext) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def chat_completions_stream(self, payload: dict[str, Any], ctx: ProviderRequestContext) -> AsyncIterator[bytes]:  # pragma: no cover
        raise NotImplementedError
        yield  # pragma: no cover

    async def embeddings(self, payload: dict[str, Any], ctx: ProviderRequestContext) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError
