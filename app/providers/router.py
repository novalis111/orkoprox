from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator
from uuid import uuid4

from app.config import Settings
from app.logging_utils import (
    _provider_response_excerpt,
    _provider_status_of,
    log_event,
)
from app.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderRequestContext,
)
from app.providers.capability_matrix import (
    model_supports_vision,
    resolve_provider_capability_profile,
)
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.reranker import RerankerProvider
from app.providers.stub import StubProvider

logger = logging.getLogger("llm-unified-proxy")


PROVIDER_ALIASES = {
    "ovh": "ovh",
    "ovh_ai_endpoints": "ovh",
    "mistral_lp": "mistral_lp",
    "mistral_la_plateforme": "mistral_lp",
    "reranker": "reranker",
    "stub": "stub",
}

# Clients die mit OpenAI-Model-Namen sprechen (Legacy-Integrationen) werden
# auf unsere Tier-Aliase umgeleitet, damit sie die neuen OVH-Modelle treffen.
# W3-Pre.4 (2026-05-04): dall-e-* + whisper-1 + tts-1 ergaenzt fuer
# vollstaendige OpenAI-Spec-Coverage. Image-Gen → SDXL, Whisper → OVH turbo.
# tts-1 hat keinen OVH-Pendant (501 Not Implemented), Konsumenten muessen
# auf lokales Coqui/Piper-Sidecar fallen oder TTS deferreren.
OPENAI_COMPAT_MODEL_ALIASES = {
    # Echte OpenAI-Modellnamen die SDKs als Default schicken — auf unsere
    # OVH-Aliases umleiten, damit alte openai-python-Clients nicht 404 sehen.
    "dall-e-2": "image",
    "dall-e-3": "image",
    "gpt-image-1": "image",
    "whisper-1": "voice",
}


@dataclass
class _ProviderState:
    cooldown_until: float = 0.0


@dataclass(frozen=True)
class _RouteDecision:
    preferred_provider: str
    preferred_model: str
    route_key: str


@dataclass(frozen=True)
class _RequestCapabilities:
    stream: bool = False
    tools: bool = False
    parallel_tool_calls: bool = False
    response_format: bool = False
    # Multimodal: True wenn der Payload mind. einen image_url-Content enthaelt.
    # Loest Capability-Filter aus, der Provider/Modelle ohne Vision skipt.
    vision: bool = False

    def missing_from(self, provider_capabilities: ProviderCapabilities) -> list[str]:
        missing: list[str] = []
        if self.stream and not provider_capabilities.supports_stream:
            missing.append("stream")
        if self.tools and not provider_capabilities.supports_tools:
            missing.append("tools")
        if self.parallel_tool_calls and not provider_capabilities.supports_parallel_tool_calls:
            missing.append("parallel_tool_calls")
        if self.response_format and not provider_capabilities.supports_response_format:
            missing.append("response_format")
        if self.vision and not provider_capabilities.supports_vision:
            missing.append("vision")
        return missing

    def as_list(self) -> list[str]:
        required: list[str] = []
        if self.stream:
            required.append("stream")
        if self.tools:
            required.append("tools")
        if self.parallel_tool_calls:
            required.append("parallel_tool_calls")
        if self.response_format:
            required.append("response_format")
        if self.vision:
            required.append("vision")
        return required


class ProviderRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._providers: dict[str, BaseProvider] = {}
        self._state: dict[str, _ProviderState] = {}

    def _build_provider(self, name: str) -> BaseProvider:
        if name == "ovh":
            return OpenAICompatibleProvider(
                name="ovh",
                base_url=self.settings.ovh_base_url,
                api_key=self.settings.ovh_api_key,
                default_model=self.settings.ovh_model,
                timeout_s=self.settings.request_timeout_seconds,
            )
        if name == "mistral_lp":
            # Mistral La Plateforme — premium provider.
            # OpenAI-spec-compatible. EU DC (Paris). Bearer token via
            # mistral_lp_api_key. Accessed via aliases `report_premium` /
            # `report_structure`. Pricing ~19.4× higher than OVH Mistral Small —
            # intentional premium path, not the default.
            return OpenAICompatibleProvider(
                name="mistral_lp",
                base_url=self.settings.mistral_lp_base_url,
                api_key=self.settings.mistral_lp_api_key,
                default_model=self.settings.mistral_lp_default_model,
                timeout_s=self.settings.request_timeout_seconds,
            )
        if name == "reranker":
            # TEI-Sidecar fuer Cross-Encoder-Reranking (bge-reranker-v2-m3).
            # Analog zum Whisper-Sidecar — ein dediziertes Self-Hosted-Modell
            # fuer eine Faehigkeit, die kein Cloud-Provider anbietet.
            return RerankerProvider(
                name="reranker",
                base_url=self.settings.reranker_base_url,
                model=self.settings.reranker_model,
                timeout_s=self.settings.reranker_timeout_s,
            )
        # Custom OpenAI-compatible providers (Baseten, Groq, Together, …)
        # registered via CUSTOM_PROVIDERS. Any OpenAI-API-shaped backend works
        # without a code change.
        custom = self.settings.custom_provider_configs().get(name)
        if custom is not None:
            return OpenAICompatibleProvider(
                name=name,
                base_url=custom["base_url"],
                api_key=custom["api_key"],
                default_model=custom["default_model"],
                timeout_s=self.settings.request_timeout_seconds,
            )
        return StubProvider()

    def get_provider(self, name: str) -> BaseProvider:
        canonical = PROVIDER_ALIASES.get(name, name)
        if canonical not in self._providers:
            self._providers[canonical] = self._build_provider(canonical)
            self._state[canonical] = _ProviderState()
        return self._providers[canonical]

    @staticmethod
    def _normalize_requested_model(raw: str) -> str:
        value = (raw or "").strip()
        if not value:
            return ""
        if value.lower() in {"free-model", "free_model", "default", "auto"}:
            return ""
        return value

    # All known aliases (tier-based + task-based + media)
    _ALL_ALIASES = {
        "xhigh",
        "high",
        "medium",
        "low",
        "classify",
        "extract",
        "compose",
        "chat",
        "reason",
        "report",
        "ocr",
        "vision",
        "vision_x",
        "image",
        "voice",
        "voice_hq",
        "reason_lite",
        "long_context",
        "reason_mid",
        # Mistral-LP premium aliases
        "report_premium",
        "report_structure",
    }

    def _route_key(self, raw_model: str) -> str:
        normalized = (raw_model or "").strip()
        alias = normalized.lower()
        if alias in ProviderRegistry._ALL_ALIASES:
            return alias
        if "/" in normalized:
            maybe_prefix, _ = normalized.split("/", 1)
            if maybe_prefix in PROVIDER_ALIASES or maybe_prefix in self._custom_provider_names():
                return "prefixed"
        return "default"

    def _custom_provider_names(self) -> set[str]:
        return set(self.settings.custom_provider_configs().keys())

    def _resolve_route_decision(self, raw_model: str) -> _RouteDecision:
        normalized_raw = (raw_model or "").strip()
        alias = normalized_raw.lower()
        alias_target = OPENAI_COMPAT_MODEL_ALIASES.get(alias)
        alias = alias_target if isinstance(alias_target, str) else alias
        if alias in self._ALL_ALIASES:
            route_key = alias
        elif isinstance(alias_target, str) and "/" in alias_target:
            route_key = "prefixed"
        else:
            route_key = self._route_key(normalized_raw)
        tier_alias_target = {
            "xhigh": self.settings.model_alias_xhigh,
            "high": self.settings.model_alias_high,
            "medium": self.settings.model_alias_medium,
            "low": self.settings.model_alias_low,
            "classify": self.settings.model_alias_classify,
            "extract": self.settings.model_alias_extract,
            "compose": self.settings.model_alias_compose,
            "chat": self.settings.model_alias_chat,
            "reason": self.settings.model_alias_reason,
            "report": self.settings.model_alias_report,
            "ocr": self.settings.model_alias_ocr,
            "vision": self.settings.model_alias_vision,
            "vision_x": self.settings.model_alias_vision_x,
            "image": self.settings.model_alias_image,
            "voice": self.settings.model_alias_voice,
            "voice_hq": self.settings.model_alias_voice_hq,
            "reason_lite": self.settings.model_alias_reason_lite,
            "long_context": self.settings.model_alias_long_context,
            "reason_mid": self.settings.model_alias_reason_mid,
            "report_premium": self.settings.model_alias_report_premium,
            "report_structure": self.settings.model_alias_report_structure,
        }.get(alias)
        if tier_alias_target and tier_alias_target.lower() not in self._ALL_ALIASES:
            normalized_raw = tier_alias_target.strip()
        elif alias_target and alias not in self._ALL_ALIASES:
            normalized_raw = alias_target.strip()

        # For non-tier, non-prefixed requests use low alias by default.
        if route_key == "default":
            low_target = (self.settings.model_alias_low or "").strip()
            if low_target:
                normalized_raw = low_target

        if "/" in normalized_raw:
            maybe_prefix, model = normalized_raw.split("/", 1)
            if maybe_prefix in PROVIDER_ALIASES:
                return _RouteDecision(
                    preferred_provider=PROVIDER_ALIASES[maybe_prefix],
                    preferred_model=self._normalize_requested_model(model),
                    route_key=route_key,
                )
            if maybe_prefix in self._custom_provider_names():
                return _RouteDecision(
                    preferred_provider=maybe_prefix,
                    preferred_model=self._normalize_requested_model(model),
                    route_key=route_key,
                )
        return _RouteDecision(
            preferred_provider=PROVIDER_ALIASES.get(
                self.settings.effective_primary_provider, "stub"
            ),
            preferred_model=self._normalize_requested_model(normalized_raw),
            route_key=route_key,
        )

    def _resolve_route(self, raw_model: str) -> tuple[str, str]:
        decision = self._resolve_route_decision(raw_model)
        return decision.preferred_provider, decision.preferred_model

    def _fallback_chain(self, preferred_provider: str) -> list[str]:
        chain = [preferred_provider]
        for provider in self.settings.fallback_provider_list:
            canonical = PROVIDER_ALIASES.get(provider, provider)
            if canonical not in chain:
                chain.append(canonical)
        return chain

    def _fallback_chain_for_route(self, preferred_provider: str, route_key: str) -> list[str]:
        chain = [preferred_provider]
        for provider in self.settings.fallback_provider_list_for_route(route_key):
            canonical = PROVIDER_ALIASES.get(provider, provider)
            if canonical not in chain:
                chain.append(canonical)
        return chain

    def _fallback_chain_for_request(self, preferred_provider: str, raw_model: str) -> list[str]:
        route = self._resolve_route_decision(raw_model)
        return self._fallback_chain_for_route(preferred_provider, route.route_key)

    def _default_model_for_provider(
        self,
        provider_name: str,
        *,
        prefer_fast: bool = False,
        is_embedding: bool = False,
        is_vision: bool = False,
    ) -> str:
        if provider_name == "ovh":
            if is_embedding:
                return self.settings.ovh_embedding_model
            if is_vision:
                return self.settings.ovh_vision_model
            return self.settings.ovh_model
        if provider_name == "mistral_lp":
            # Mistral-LP hat keine eigenen Embedding/Vision-Defaults — wer
            # Embeddings braucht, geht über OVH; Pixtral (Vision) muss
            # explizit angefordert werden.
            if is_embedding or is_vision:
                return ""
            return self.settings.mistral_lp_default_model
        return ""

    @staticmethod
    def _payload_has_image(payload: dict[str, Any]) -> bool:
        """True wenn irgendeine Message einen image_url-Content-Block enthaelt.

        OpenAI-Format: messages[i].content kann list von Parts sein, jede Part
        hat type="text" oder type="image_url". Wir suchen die zweite Variante.
        Auch type="input_image" (neuere OpenAI-Konvention) wird erkannt.
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type in ("image_url", "input_image", "image"):
                    return True
        return False

    @classmethod
    def _request_capabilities(
        cls, payload: dict[str, Any], *, stream: bool
    ) -> _RequestCapabilities:
        tools = payload.get("tools")
        has_tools = isinstance(tools, list) and len(tools) > 0
        return _RequestCapabilities(
            stream=stream,
            tools=has_tools,
            parallel_tool_calls=has_tools and bool(payload.get("parallel_tool_calls")),
            response_format=isinstance(payload.get("response_format"), dict),
            vision=cls._payload_has_image(payload),
        )

    @staticmethod
    def _provider_missing_capabilities(
        provider_name: str,
        provider: BaseProvider,
        required: _RequestCapabilities,
        overrides: dict[str, ProviderCapabilities] | None = None,
    ) -> list[str]:
        profile = resolve_provider_capability_profile(provider_name, provider, overrides=overrides)
        return required.missing_from(profile.capabilities)

    @staticmethod
    def _model_missing_capabilities(
        provider_name: str,
        model_name: str,
        required: _RequestCapabilities,
    ) -> list[str]:
        """Pro-Modell-Capability-Check (feiner als Provider-Level).

        Aktuell nur Vision: ein Provider mit supports_vision=True hat oft nur
        eines seiner Modelle vision-fähig (OVH: Mistral-Small + Qwen2.5-VL).
        Wer ein anderes Modell + Bild schickt → mismatch hier, nicht 502 vom
        Upstream.
        """
        missing: list[str] = []
        if required.vision and not model_supports_vision(provider_name, model_name):
            missing.append("vision")
        return missing

    # Task aliases that should prefer fast non-reasoning models on fallback
    _FAST_TASK_ALIASES = {"classify", "extract", "compose", "chat"}
    # Vision/OCR aliases route to vision-specific defaults on fallback.
    _VISION_ALIASES = {"ocr", "vision"}

    def _model_for_candidate(
        self,
        preferred_provider: str,
        candidate_provider: str,
        preferred_model: str,
        route_key: str = "",
    ) -> str:
        # Keep explicit model on preferred provider; on fallback switch to provider default.
        if candidate_provider == preferred_provider:
            if preferred_model and preferred_model.strip():
                return preferred_model
            return self._default_model_for_provider(
                candidate_provider, is_vision=route_key in self._VISION_ALIASES
            )
        # Task aliases prefer fast non-reasoning models on Together.ai fallback
        prefer_fast = route_key in self._FAST_TASK_ALIASES
        return self._default_model_for_provider(
            candidate_provider,
            prefer_fast=prefer_fast,
            is_vision=route_key in self._VISION_ALIASES,
        )

    def _is_cooling_down(self, provider_name: str, *, scope: str = "") -> bool:
        key = f"{provider_name}:{scope}" if scope else provider_name
        state = self._state.get(key)
        return bool(state and state.cooldown_until > time.time())

    def _apply_model_policy(
        self,
        model_name: str,
        payload: dict[str, Any],
        *,
        ctx: ProviderRequestContext | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Modell-Policy: payload pass-through, kein max_tokens-Override.

        OVH AI Endpoints respektieren den client-`max_tokens`-Wert nativ
        (mit ihrem eigenen Modell-Cap). Die alte Apriel-Policy (forced
        100k max_tokens + json_object response_format + injizierter
        Strict-Prompt) und der Non-Apriel-Floor (raised <1000 → 1000)
        have been removed — Apriel is no longer active, and the floor was
        a contributing cause of OVH 422 cascades on json_schema calls with
        max_tokens=900.

        Health-Probe-Bypass bleibt als Marker (für Logging/Telemetrie),
        aber ohne Verhaltens-Override — OVH liefert auf max_tokens=5
        sofort zurück.
        """
        policy: dict[str, Any] = {}
        if ctx is not None and getattr(ctx, "is_health_probe", False):
            policy["health_probe_bypass"] = True
        return dict(payload), policy

    @staticmethod
    def _lift_reasoning_to_content(data: dict[str, Any]) -> bool:
        """OVH-Reasoning-Models packen Output ins `reasoning`-Feld statt `content`.

        Observed with `gpt-oss-20b` and `Qwen3.5-9B`: OVH returns HTTP 200
        with `message.reasoning` (or `message.reasoning_content`) populated
        but `message.content=""` — clients see an empty response, the proxy
        logs `empty_content`.
        Das ist OVH-/Modell-spezifischer Spec-Drift, kein OpenAI-Standard.

        Fix: Wenn `content` leer und `reasoning`/`reasoning_content` gefuellt,
        kopiere die Reasoning-Tokens nach `content` (ohne den `<think>`-Block,
        falls einer drin ist). Das urspruengliche Reasoning-Feld bleibt fuer
        Telemetrie/Audit erhalten. Konsumenten lesen aus `content` wie bisher
        — Cross-Repo-konsistent.

        Returns True wenn ein Lift stattgefunden hat.
        """
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message", {})
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return False  # content schon gefuellt, kein Lift noetig
        # Tool-Calls: legitime Antwort ohne content-Text
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return False
        # Reasoning-Felder als Fallback. OVH benutzt beide Schreibweisen.
        for reasoning_field in ("reasoning_content", "reasoning"):
            value = message.get(reasoning_field)
            if isinstance(value, str) and value.strip():
                # Strip <think>...</think>-Wrapper falls vorhanden (Qwen3-Style)
                stripped = value.strip()
                if stripped.startswith("<think>"):
                    end = stripped.find("</think>")
                    if end != -1:
                        stripped = stripped[end + len("</think>") :].strip()
                    else:
                        # Kein Schluss-Tag → ganzes reasoning als content
                        stripped = stripped[len("<think>") :].strip()
                if stripped:
                    message["content"] = stripped
                    return True
        return False

    @staticmethod
    def _is_empty_content(data: dict[str, Any]) -> bool:
        """Detect null/empty content in provider response (Provider-Bug-Schutz).

        Hinweis: vor diesem Check sollte `_lift_reasoning_to_content()` laufen,
        damit OVH-Reasoning-Modelle (gpt-oss-20b, Qwen3.5-9B, Qwen3-32B) deren
        Output im `reasoning`-Feld liegt nicht faelschlich als empty markiert
        werden.
        """
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return True
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message", {})
        if not isinstance(message, dict):
            return True
        content = message.get("content")
        # Tool call responses legitimately have no content
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return False
        return content is None or (isinstance(content, str) and not content.strip())

    def _cooldown_seconds_for(self, scope: str, exc: ProviderError | None) -> int:
        """Differentiated cooldown per scope and error type.

        - Rate-limit (HTTP 429): immer langer Cooldown (Free-Tier-Schutz, z.B. Groq).
        - OCR/Vision-Scope: kurzer Cooldown (10s default), da Batch-OCR 8-fach
          parallel pumpt und ein einzelner transient Fehler nicht den ganzen
          Provider 30s lang sperren darf.
        - Sonst: globaler Default (30s).
        """
        if exc is not None and exc.status_code == 429:
            return int(self.settings.provider_cooldown_seconds_rate_limited)
        if scope in {"ocr", "vision"}:
            return int(self.settings.provider_cooldown_seconds_ocr)
        return int(self.settings.provider_cooldown_seconds)

    # Request-spezifische Validierungsfehler des Upstreams. Ein kaputter
    # Payload EINES Consumers (z.B. Prompt > Kontextfenster -> OVH 400
    # "max_tokens must be at least 1") darf NICHT den Provider fuer ALLE
    # sperren. Diese Stati signalisieren "diese Anfrage ist ungueltig",
    # nicht "der Provider ist down" -> kein Cooldown.
    # Cooldown bleibt fuer: 429 (eigener Pfad), 5xx, Timeout/ConnectionError
    # (504/502 upstream_*), Auth-Fehler (401/403).
    _PAYLOAD_ERROR_STATUSES: frozenset[int] = frozenset({400, 404, 413, 422})

    def _start_cooldown(
        self,
        provider_name: str,
        *,
        scope: str = "",
        exc: ProviderError | None = None,
    ) -> None:
        # Never cooldown the stub — it always fails by design, and cooldown
        # would poison the "all providers in cooldown" error path.
        if provider_name == "stub":
            return
        # No cooldown for request-specific payload validation errors: a single
        # consumer's broken request must not lock the provider for everyone.
        if exc is not None:
            provider_status = (
                (exc.details or {}).get("provider_status")
                if isinstance(exc.details, dict)
                else None
            )
            if provider_status in self._PAYLOAD_ERROR_STATUSES:
                log_event(
                    logger,
                    "cooldown_skipped_payload_error",
                    provider=provider_name,
                    scope=scope or "global",
                    provider_status=provider_status,
                    error_code=exc.code,
                    error_message=str(exc)[:500],
                )
                return
        # Scope-based cooldowns: an embedding failure must NOT block chat calls.
        # Key format: "provider_name:scope" (e.g. "ovh:embeddings").
        # Empty scope = legacy global cooldown for backward compat.
        key = f"{provider_name}:{scope}" if scope else provider_name
        state = self._state.setdefault(key, _ProviderState())
        state.cooldown_until = time.time() + self._cooldown_seconds_for(scope, exc)

    def _should_try_ollama_default_model(
        self,
        candidate_provider: str,
        attempted_model: str,
        exc: ProviderError,
    ) -> bool:
        """
        Retry once on Ollama with configured default model when explicit model is missing.
        """
        if candidate_provider != "ollama":
            return False
        fallback_model = self._default_model_for_provider("ollama")
        if not fallback_model or fallback_model == attempted_model:
            return False

        text = str(exc).lower()
        provider_response = (
            ((exc.details or {}).get("provider_response") or {})
            if isinstance(exc.details, dict)
            else {}
        )
        response_blob = json.dumps(provider_response, ensure_ascii=True).lower()
        haystack = f"{text} {response_blob}"
        return all(marker in haystack for marker in ("model", "not found")) or (
            "unknown model" in haystack
        )

    async def _run_with_retry(
        self, provider: BaseProvider, fn_name: str, *args: Any, **kwargs: Any
    ):
        attempts = self.settings.provider_max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                fn = getattr(provider, fn_name)
                return await fn(*args, **kwargs)
            except ProviderError as exc:
                last_exc = exc
                if not exc.retryable or attempt == attempts - 1:
                    break
                # Exponentielles Backoff mit cap (P3-6, BUNDLE-A).
                await asyncio.sleep(
                    min(
                        self.settings.provider_retry_backoff_seconds * (2**attempt),
                        self.settings.provider_retry_backoff_cap_seconds,
                    )
                )

        if isinstance(last_exc, ProviderError):
            raise last_exc
        raise ProviderError("provider request failed", retryable=False)

    async def chat_completions(
        self,
        raw_model: str,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        route = self._resolve_route_decision(raw_model)
        provider_name = route.preferred_provider
        resolved_model = route.preferred_model
        fallback_chain = self._fallback_chain_for_route(provider_name, route.route_key)
        required_capabilities = self._request_capabilities(payload, stream=False)

        last_error: ProviderError | None = None
        capability_error: ProviderError | None = None
        for candidate in fallback_chain:
            provider = self.get_provider(candidate)
            capability_profile = resolve_provider_capability_profile(
                candidate,
                provider,
                overrides=self.settings.provider_capability_overrides_map,
            )
            missing_capabilities = self._provider_missing_capabilities(
                candidate,
                provider,
                required_capabilities,
                overrides=self.settings.provider_capability_overrides_map,
            )
            if missing_capabilities:
                log_event(
                    logger,
                    "route_candidate_skipped",
                    request_id=ctx.request_id,
                    stream=False,
                    raw_model=raw_model,
                    resolved_provider=provider_name,
                    resolved_model=resolved_model,
                    fallback_chain=fallback_chain,
                    candidate_provider=candidate,
                    reason="capability_mismatch",
                    candidate_capabilities=capability_profile.capabilities.__dict__,
                    candidate_verification_level=capability_profile.verification_level,
                    missing_capabilities=missing_capabilities,
                    required_capabilities=required_capabilities.as_list(),
                )
                capability_error = ProviderError(
                    f"{candidate} does not support required capabilities: {', '.join(missing_capabilities)}",
                    status_code=400,
                    code="unsupported_capabilities",
                    retryable=False,
                    details={
                        "candidate_provider": candidate,
                        "candidate_capabilities": capability_profile.capabilities.__dict__,
                        "candidate_verification_level": capability_profile.verification_level,
                        "missing_capabilities": missing_capabilities,
                        "required_capabilities": required_capabilities.as_list(),
                    },
                )
                continue
            candidate_model = self._model_for_candidate(
                preferred_provider=provider_name,
                candidate_provider=candidate,
                preferred_model=resolved_model,
                route_key=route.route_key,
            )
            # Modell-Level-Capability-Check (z.B. OVH/Llama-70B + Bild → mismatch,
            # weil Llama-70B kein Vision-Modell ist).
            # Nur wenn der Provider-Level-Check (oben) durchgewunken hat — sonst
            # wuerde der Modell-Check denselben Mismatch doppelt loggen.
            model_missing = self._model_missing_capabilities(
                candidate, candidate_model, required_capabilities
            )
            if model_missing:
                log_event(
                    logger,
                    "route_candidate_skipped",
                    request_id=ctx.request_id,
                    stream=False,
                    raw_model=raw_model,
                    resolved_provider=provider_name,
                    resolved_model=resolved_model,
                    fallback_chain=fallback_chain,
                    candidate_provider=candidate,
                    candidate_model=candidate_model,
                    reason="model_capability_mismatch",
                    missing_capabilities=model_missing,
                    required_capabilities=required_capabilities.as_list(),
                )
                capability_error = ProviderError(
                    f"{candidate}/{candidate_model} model does not support: {', '.join(model_missing)}",
                    status_code=400,
                    code="unsupported_model_capabilities",
                    retryable=False,
                    details={
                        "candidate_provider": candidate,
                        "candidate_model": candidate_model,
                        "missing_capabilities": model_missing,
                        "required_capabilities": required_capabilities.as_list(),
                    },
                )
                continue
            candidate_payload = dict(payload)
            candidate_payload["model"] = candidate_model
            candidate_payload, policy_debug = self._apply_model_policy(
                candidate_model, candidate_payload, ctx=ctx
            )
            if self._is_cooling_down(candidate, scope=route.route_key):
                log_event(
                    logger,
                    "route_candidate_skipped",
                    request_id=ctx.request_id,
                    stream=False,
                    raw_model=raw_model,
                    resolved_provider=provider_name,
                    resolved_model=resolved_model,
                    fallback_chain=fallback_chain,
                    candidate_provider=candidate,
                    candidate_model=candidate_model,
                    reason="cooldown_active",
                )
                last_error = ProviderError(
                    f"{candidate} is in cooldown",
                    code="provider_cooldown_active",
                    retryable=True,
                )
                continue
            try:
                attempt_model = candidate_model
                attempt_payload = dict(candidate_payload)
                attempt_policy = dict(policy_debug)
                log_event(
                    logger,
                    "route_candidate_attempt",
                    request_id=ctx.request_id,
                    stream=False,
                    raw_model=raw_model,
                    resolved_provider=provider_name,
                    resolved_model=resolved_model,
                    fallback_chain=fallback_chain,
                    candidate_provider=candidate,
                    candidate_model=attempt_model,
                    candidate_capabilities=capability_profile.capabilities.__dict__,
                    candidate_verification_level=capability_profile.verification_level,
                    policy=attempt_policy,
                )
                data = await self._run_with_retry(
                    provider, "chat_completions", attempt_payload, ctx
                )
                # OVH-Reasoning-Modelle (gpt-oss-20b, Qwen3.5-9B, teils Qwen3-32B)
                # liefern Output in `message.reasoning` statt `message.content`.
                # Lift VOR empty-Check, sonst werden valide Antworten als empty
                # interpretiert und triggern unnoetigen Fallback + Cooldown.
                # Lift before empty-check to avoid spurious fallback + cooldown.
                if self._lift_reasoning_to_content(data):
                    log_event(
                        logger,
                        "route_reasoning_lifted",
                        request_id=ctx.request_id,
                        raw_model=raw_model,
                        candidate_provider=candidate,
                        candidate_model=attempt_model,
                    )
                # Guard: treat null/empty content as provider failure → trigger fallback.
                # Mancher Provider liefert HTTP 200 + content=null (z.B. bei zu kleinen max_tokens).
                if self._is_empty_content(data):
                    log_event(
                        logger,
                        "route_candidate_failed",
                        request_id=ctx.request_id,
                        stream=False,
                        raw_model=raw_model,
                        resolved_provider=provider_name,
                        resolved_model=resolved_model,
                        fallback_chain=fallback_chain,
                        candidate_provider=candidate,
                        candidate_model=attempt_model,
                        error_code="empty_content",
                        error_status=200,
                        error_message="Provider returned null/empty content",
                        policy=attempt_policy,
                    )
                    last_error = ProviderError(
                        f"{candidate} returned empty content",
                        status_code=502,
                        code="empty_content",
                        retryable=True,
                    )
                    self._start_cooldown(candidate, scope=route.route_key, exc=last_error)
                    continue
                route_debug = {
                    "raw_model": raw_model,
                    "resolved_provider": provider_name,
                    "resolved_model": resolved_model,
                    "fallback_chain": fallback_chain,
                    "required_capabilities": required_capabilities.as_list(),
                }
                log_event(
                    logger,
                    "route_candidate_success",
                    request_id=ctx.request_id,
                    stream=False,
                    raw_model=raw_model,
                    resolved_provider=provider_name,
                    resolved_model=resolved_model,
                    selected_provider=provider.name,
                    selected_model=attempt_model,
                    fallback_chain=fallback_chain,
                    policy=attempt_policy,
                )
                return provider.name, attempt_model, data, route_debug
            except ProviderError as exc:
                if self._should_try_ollama_default_model(candidate, candidate_model, exc):
                    fallback_model = self._default_model_for_provider("ollama")
                    retry_payload = dict(candidate_payload)
                    retry_payload["model"] = fallback_model
                    retry_payload, retry_policy = self._apply_model_policy(
                        fallback_model, retry_payload, ctx=ctx
                    )
                    retry_policy["ollama_missing_model_fallback_from"] = candidate_model
                    retry_policy["ollama_missing_model_fallback_to"] = fallback_model
                    log_event(
                        logger,
                        "route_candidate_attempt",
                        request_id=ctx.request_id,
                        stream=False,
                        raw_model=raw_model,
                        resolved_provider=provider_name,
                        resolved_model=resolved_model,
                        fallback_chain=fallback_chain,
                        candidate_provider=candidate,
                        candidate_model=fallback_model,
                        policy=retry_policy,
                    )
                    try:
                        data = await self._run_with_retry(
                            provider, "chat_completions", retry_payload, ctx
                        )
                        route_debug = {
                            "raw_model": raw_model,
                            "resolved_provider": provider_name,
                            "resolved_model": resolved_model,
                            "fallback_chain": fallback_chain,
                            "required_capabilities": required_capabilities.as_list(),
                        }
                        log_event(
                            logger,
                            "route_candidate_success",
                            request_id=ctx.request_id,
                            stream=False,
                            raw_model=raw_model,
                            resolved_provider=provider_name,
                            resolved_model=resolved_model,
                            selected_provider=provider.name,
                            selected_model=fallback_model,
                            fallback_chain=fallback_chain,
                            policy=retry_policy,
                        )
                        return provider.name, fallback_model, data, route_debug
                    except ProviderError as retry_exc:
                        exc = retry_exc
                last_error = exc
                self._start_cooldown(candidate, scope=route.route_key, exc=exc)
                log_event(
                    logger,
                    "route_candidate_failed",
                    request_id=ctx.request_id,
                    stream=False,
                    raw_model=raw_model,
                    resolved_provider=provider_name,
                    resolved_model=resolved_model,
                    fallback_chain=fallback_chain,
                    candidate_provider=candidate,
                    candidate_model=candidate_model,
                    candidate_capabilities=capability_profile.capabilities.__dict__,
                    candidate_verification_level=capability_profile.verification_level,
                    error_code=exc.code,
                    error_status=exc.status_code,
                    error_message=str(exc),
                    provider_status=_provider_status_of(exc),
                    provider_response=_provider_response_excerpt(exc),
                    policy=policy_debug,
                )
                continue

        raise (
            last_error
            or capability_error
            or ProviderError("no provider available", status_code=503, code="provider_unavailable")
        )

    # Providers that have dedicated embedding models configured.
    # OVH is primary, ollama is fallback. No US-based embedding providers
    # are included (EU AI Act compliance). Local dev overrides via ENV.
    _EMBEDDING_PROVIDERS: tuple[str, ...] = ("ovh", "ollama")

    async def embeddings(
        self,
        raw_model: str,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        # Embedding routing is SEPARATE from chat routing.
        # Only providers with dedicated embedding models are tried.
        # Only providers with dedicated embedding models (OVH + Ollama) are tried.
        fallback_chain = [
            p
            for p in self._EMBEDDING_PROVIDERS
            if self._default_model_for_provider(p, is_embedding=True)
        ]
        if not fallback_chain:
            raise ProviderError(
                "no embedding provider configured",
                status_code=503,
                code="no_embedding_provider",
            )

        last_error: ProviderError | None = None
        for candidate in fallback_chain:
            if self._is_cooling_down(candidate, scope="embeddings"):
                log_event(
                    logger,
                    "embedding_candidate_skipped",
                    request_id=ctx.request_id,
                    stream=False,
                    raw_model=raw_model,
                    candidate_provider=candidate,
                    reason="cooldown_active",
                )
                continue

            provider = self.get_provider(candidate)
            candidate_model = self._default_model_for_provider(candidate, is_embedding=True)
            candidate_payload = dict(payload)
            candidate_payload["model"] = candidate_model

            try:
                data = await self._run_with_retry(provider, "embeddings", candidate_payload, ctx)
                route_debug = {
                    "raw_model": raw_model,
                    "selected_provider": candidate,
                    "selected_model": candidate_model,
                    "fallback_chain": fallback_chain,
                }
                log_event(
                    logger,
                    "embedding_success",
                    request_id=ctx.request_id,
                    stream=False,
                    raw_model=raw_model,
                    selected_provider=candidate,
                    selected_model=candidate_model,
                )
                return provider.name, candidate_model, data, route_debug
            except ProviderError as exc:
                log_event(
                    logger,
                    "embedding_candidate_failed",
                    request_id=ctx.request_id,
                    stream=False,
                    raw_model=raw_model,
                    candidate_provider=candidate,
                    candidate_model=candidate_model,
                    error_code=exc.code,
                    error_status=exc.status_code,
                    error_message=str(exc),
                    provider_status=_provider_status_of(exc),
                    provider_response=_provider_response_excerpt(exc),
                )
                last_error = exc
                self._start_cooldown(candidate, scope="embeddings", exc=exc)
                continue

        raise (
            last_error
            or ProviderError(
                "no embedding provider available",
                status_code=503,
                code="embedding_provider_unavailable",
            )
        )

    async def rerank(
        self,
        raw_model: str,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        """Cross-Encoder-Reranking via TEI-Sidecar.

        Es gibt nur EINEN Reranker-Provider (das `reranker`-Sidecar) — keine
        Fallback-Kette noetig. Bei Sidecar-Down propagiert die klare
        ProviderError (`reranker_unavailable`) sauber nach oben.
        """
        if not self.settings.reranker_enabled:
            raise ProviderError(
                "reranking is disabled",
                status_code=503,
                code="reranker_disabled",
            )

        provider = self.get_provider("reranker")
        candidate_model = self.settings.reranker_model
        candidate_payload = dict(payload)
        candidate_payload["model"] = candidate_model

        try:
            data = await provider.rerank(candidate_payload, ctx)
        except ProviderError as exc:
            log_event(
                logger,
                "rerank_failed",
                request_id=ctx.request_id,
                stream=False,
                raw_model=raw_model,
                selected_provider="reranker",
                selected_model=candidate_model,
                error_code=exc.code,
                error_status=exc.status_code,
                error_message=str(exc),
            )
            raise

        route_debug = {
            "raw_model": raw_model,
            "selected_provider": "reranker",
            "selected_model": candidate_model,
            "fallback_chain": ["reranker"],
        }
        log_event(
            logger,
            "rerank_success",
            request_id=ctx.request_id,
            stream=False,
            raw_model=raw_model,
            selected_provider="reranker",
            selected_model=candidate_model,
        )
        return provider.name, candidate_model, data, route_debug

    async def chat_completions_stream(
        self,
        raw_model: str,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> tuple[str, str, AsyncIterator[bytes], dict[str, Any]]:
        route = self._resolve_route_decision(raw_model)
        provider_name = route.preferred_provider
        resolved_model = route.preferred_model
        fallback_chain = self._fallback_chain_for_route(provider_name, route.route_key)
        required_capabilities = self._request_capabilities(payload, stream=True)

        last_error: ProviderError | None = None
        capability_error: ProviderError | None = None

        for candidate in fallback_chain:
            provider = self.get_provider(candidate)
            capability_profile = resolve_provider_capability_profile(
                candidate,
                provider,
                overrides=self.settings.provider_capability_overrides_map,
            )
            missing_capabilities = self._provider_missing_capabilities(
                candidate,
                provider,
                required_capabilities,
                overrides=self.settings.provider_capability_overrides_map,
            )
            if missing_capabilities:
                log_event(
                    logger,
                    "route_candidate_skipped",
                    request_id=ctx.request_id,
                    stream=True,
                    raw_model=raw_model,
                    resolved_provider=provider_name,
                    resolved_model=resolved_model,
                    fallback_chain=fallback_chain,
                    candidate_provider=candidate,
                    reason="capability_mismatch",
                    candidate_capabilities=capability_profile.capabilities.__dict__,
                    candidate_verification_level=capability_profile.verification_level,
                    missing_capabilities=missing_capabilities,
                    required_capabilities=required_capabilities.as_list(),
                )
                capability_error = ProviderError(
                    f"{candidate} does not support required capabilities: {', '.join(missing_capabilities)}",
                    status_code=400,
                    code="unsupported_capabilities",
                    retryable=False,
                    details={
                        "candidate_provider": candidate,
                        "candidate_capabilities": capability_profile.capabilities.__dict__,
                        "candidate_verification_level": capability_profile.verification_level,
                        "missing_capabilities": missing_capabilities,
                        "required_capabilities": required_capabilities.as_list(),
                    },
                )
                continue
            candidate_model = self._model_for_candidate(
                preferred_provider=provider_name,
                candidate_provider=candidate,
                preferred_model=resolved_model,
                route_key=route.route_key,
            )
            # Modell-Level-Capability-Check (z.B. OVH/Llama-70B + Bild → mismatch).
            model_missing = self._model_missing_capabilities(
                candidate, candidate_model, required_capabilities
            )
            if model_missing:
                log_event(
                    logger,
                    "route_candidate_skipped",
                    request_id=ctx.request_id,
                    stream=True,
                    raw_model=raw_model,
                    resolved_provider=provider_name,
                    resolved_model=resolved_model,
                    fallback_chain=fallback_chain,
                    candidate_provider=candidate,
                    candidate_model=candidate_model,
                    reason="model_capability_mismatch",
                    missing_capabilities=model_missing,
                    required_capabilities=required_capabilities.as_list(),
                )
                capability_error = ProviderError(
                    f"{candidate}/{candidate_model} model does not support: {', '.join(model_missing)}",
                    status_code=400,
                    code="unsupported_model_capabilities",
                    retryable=False,
                    details={
                        "candidate_provider": candidate,
                        "candidate_model": candidate_model,
                        "missing_capabilities": model_missing,
                        "required_capabilities": required_capabilities.as_list(),
                    },
                )
                continue
            candidate_payload = dict(payload)
            candidate_payload["model"] = candidate_model
            candidate_payload, policy_debug = self._apply_model_policy(
                candidate_model, candidate_payload, ctx=ctx
            )
            if self._is_cooling_down(candidate, scope=route.route_key):
                log_event(
                    logger,
                    "route_candidate_skipped",
                    request_id=ctx.request_id,
                    stream=True,
                    raw_model=raw_model,
                    resolved_provider=provider_name,
                    resolved_model=resolved_model,
                    fallback_chain=fallback_chain,
                    candidate_provider=candidate,
                    candidate_model=candidate_model,
                    reason="cooldown_active",
                    policy=policy_debug,
                )
                last_error = ProviderError(
                    f"{candidate} is in cooldown",
                    code="provider_cooldown_active",
                    retryable=True,
                )
                continue

            attempts = self.settings.provider_max_retries + 1
            for attempt in range(attempts):
                try:
                    attempt_model = candidate_model
                    attempt_payload = dict(candidate_payload)
                    attempt_policy = dict(policy_debug)
                    log_event(
                        logger,
                        "route_candidate_attempt",
                        request_id=ctx.request_id,
                        stream=True,
                        raw_model=raw_model,
                        resolved_provider=provider_name,
                        resolved_model=resolved_model,
                        fallback_chain=fallback_chain,
                        candidate_provider=candidate,
                        candidate_model=attempt_model,
                        attempt=attempt + 1,
                        candidate_capabilities=capability_profile.capabilities.__dict__,
                        candidate_verification_level=capability_profile.verification_level,
                        policy=attempt_policy,
                    )
                    stream = provider.chat_completions_stream(attempt_payload, ctx)
                    first_chunk = await anext(stream)
                    route_debug = {
                        "raw_model": raw_model,
                        "resolved_provider": provider_name,
                        "resolved_model": resolved_model,
                        "fallback_chain": fallback_chain,
                        "required_capabilities": required_capabilities.as_list(),
                    }
                    log_event(
                        logger,
                        "route_candidate_success",
                        request_id=ctx.request_id,
                        stream=True,
                        raw_model=raw_model,
                        resolved_provider=provider_name,
                        resolved_model=resolved_model,
                        selected_provider=provider.name,
                        selected_model=attempt_model,
                        fallback_chain=fallback_chain,
                        policy=attempt_policy,
                    )

                    async def replay_stream(
                        first: bytes, tail: AsyncIterator[bytes]
                    ) -> AsyncIterator[bytes]:
                        yield first
                        async for chunk in tail:
                            yield chunk

                    return (
                        provider.name,
                        attempt_model,
                        replay_stream(first_chunk, stream),
                        route_debug,
                    )
                except NotImplementedError:
                    data = await self._run_with_retry(
                        provider, "chat_completions", candidate_payload, ctx
                    )
                    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

                    async def synthetic_stream() -> AsyncIterator[bytes]:
                        chunk = {
                            "id": f"chatcmpl-{uuid4()}",
                            "object": "chat.completion.chunk",
                            "model": candidate_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"role": "assistant", "content": text},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=True)}\\n\\n".encode("utf-8")
                        yield b"data: [DONE]\\n\\n"

                    route_debug = {
                        "raw_model": raw_model,
                        "resolved_provider": provider_name,
                        "resolved_model": resolved_model,
                        "fallback_chain": fallback_chain,
                    }
                    log_event(
                        logger,
                        "route_candidate_success",
                        request_id=ctx.request_id,
                        stream=True,
                        raw_model=raw_model,
                        resolved_provider=provider_name,
                        resolved_model=resolved_model,
                        selected_provider=provider.name,
                        selected_model=candidate_model,
                        fallback_chain=fallback_chain,
                        synthetic_stream=True,
                        policy=policy_debug,
                    )
                    return (
                        provider.name,
                        candidate_model,
                        synthetic_stream(),
                        route_debug,
                    )
                except StopAsyncIteration:
                    raise ProviderError(
                        f"{provider.name} returned an empty stream",
                        status_code=502,
                        code="upstream_protocol_error",
                        retryable=False,
                    )
                except ProviderError as exc:
                    if self._should_try_ollama_default_model(candidate, candidate_model, exc):
                        fallback_model = self._default_model_for_provider("ollama")
                        retry_payload = dict(candidate_payload)
                        retry_payload["model"] = fallback_model
                        retry_payload, retry_policy = self._apply_model_policy(
                            fallback_model, retry_payload, ctx=ctx
                        )
                        retry_policy["ollama_missing_model_fallback_from"] = candidate_model
                        retry_policy["ollama_missing_model_fallback_to"] = fallback_model
                        log_event(
                            logger,
                            "route_candidate_attempt",
                            request_id=ctx.request_id,
                            stream=True,
                            raw_model=raw_model,
                            resolved_provider=provider_name,
                            resolved_model=resolved_model,
                            fallback_chain=fallback_chain,
                            candidate_provider=candidate,
                            candidate_model=fallback_model,
                            attempt=attempt + 1,
                            candidate_capabilities=capability_profile.capabilities.__dict__,
                            candidate_verification_level=capability_profile.verification_level,
                            policy=retry_policy,
                        )
                        try:
                            stream = provider.chat_completions_stream(retry_payload, ctx)
                            first_chunk = await anext(stream)
                            route_debug = {
                                "raw_model": raw_model,
                                "resolved_provider": provider_name,
                                "resolved_model": resolved_model,
                                "fallback_chain": fallback_chain,
                                "required_capabilities": required_capabilities.as_list(),
                            }
                            log_event(
                                logger,
                                "route_candidate_success",
                                request_id=ctx.request_id,
                                stream=True,
                                raw_model=raw_model,
                                resolved_provider=provider_name,
                                resolved_model=resolved_model,
                                selected_provider=provider.name,
                                selected_model=fallback_model,
                                fallback_chain=fallback_chain,
                                policy=retry_policy,
                            )

                            async def replay_stream(
                                first: bytes, tail: AsyncIterator[bytes]
                            ) -> AsyncIterator[bytes]:
                                yield first
                                async for chunk in tail:
                                    yield chunk

                            return (
                                provider.name,
                                fallback_model,
                                replay_stream(first_chunk, stream),
                                route_debug,
                            )
                        except ProviderError as retry_exc:
                            exc = retry_exc
                    last_error = exc
                    should_retry = exc.retryable and attempt < attempts - 1
                    log_event(
                        logger,
                        "route_candidate_failed",
                        request_id=ctx.request_id,
                        stream=True,
                        raw_model=raw_model,
                        resolved_provider=provider_name,
                        resolved_model=resolved_model,
                        fallback_chain=fallback_chain,
                        candidate_provider=candidate,
                        candidate_model=candidate_model,
                        attempt=attempt + 1,
                        candidate_capabilities=capability_profile.capabilities.__dict__,
                        candidate_verification_level=capability_profile.verification_level,
                        will_retry=should_retry,
                        error_code=exc.code,
                        error_status=exc.status_code,
                        error_message=str(exc),
                        policy=policy_debug,
                    )
                    if should_retry:
                        # Exponentielles Backoff mit cap (P3-6, BUNDLE-A).
                        await asyncio.sleep(
                            min(
                                self.settings.provider_retry_backoff_seconds * (2**attempt),
                                self.settings.provider_retry_backoff_cap_seconds,
                            )
                        )
                        continue
                    self._start_cooldown(candidate, scope=route.route_key, exc=exc)
                    break

        raise (
            last_error
            or capability_error
            or ProviderError("no provider available", status_code=503, code="provider_unavailable")
        )

    def provider_ready(self) -> bool:
        names = [
            self.settings.effective_primary_provider,
            *self.settings.fallback_provider_list,
        ]
        for raw_name in names:
            candidate = PROVIDER_ALIASES.get(raw_name, raw_name)
            provider = self.get_provider(candidate)
            if isinstance(provider, StubProvider):
                return True
            if candidate == "ollama":
                return True
            if getattr(provider, "api_key", ""):
                return True
        return False
