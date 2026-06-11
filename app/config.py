from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.providers.base import ProviderCapabilities
from app.providers.capability_matrix import parse_capability_overrides


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    host: str = "0.0.0.0"
    port: int = 8091
    log_level: str = "INFO"
    log_json: bool = True
    log_file: str = "/tmp/orkoprox.log"
    environment: str = "development"

    # Response-header namespace for usage/quota/guard metadata. All
    # gateway-emitted headers follow the pattern ``{BRAND_HEADER_PREFIX}-<field>``
    # (e.g. ``X-Orkoprox-Usage-Pct``). Override per deployment/tenant to namespace
    # the gateway behind your own brand without touching code.
    brand_header_prefix: str = "X-Orkoprox"

    proxy_auth_required: bool = True
    proxy_api_keys: str = ""
    production_min_api_key_length: int = 40

    # Reject request bodies larger than this (bytes) with HTTP 413 before they
    # are read into memory — guards against a tenant exhausting RAM ahead of the
    # upstream call. Default 10 MiB covers large chat/embeddings payloads.
    # Set to 0 to disable the check.
    max_request_body_bytes: int = 10 * 1024 * 1024

    # ── Admin plane (strictly separate from the data plane) ────────────────
    # Keys that may call /v1/admin/* (key management, tenant usage). These are
    # NOT accepted on data-plane (/v1/chat, …) requests, and data-plane keys are
    # NEVER accepted on the admin plane — a data key can never manage keys.
    # Secure by default: if empty, the admin endpoints are disabled (403), not
    # open. Comma-separated.
    admin_api_keys: str = ""

    # ── Per-key request rate limiting ──────────────────────────────────────
    # Token quotas alone don't protect against burst/DoS. These cap request
    # rate and in-flight concurrency per API key. 0 = unlimited (disabled).
    rate_limit_per_minute: int = 0
    rate_limit_concurrency: int = 0
    # Burst allowance on top of the per-minute rate (token-bucket capacity).
    rate_limit_burst: int = 0

    # ── Audit log (append-only) ────────────────────────────────────────────
    # Records who/when/which-key-prefix/which-model/cost. NEVER the full key and
    # NEVER prompt content (privacy by default — request bodies are not logged).
    audit_log_enabled: bool = False
    audit_log_path: str = "/tmp/orkoprox-audit.jsonl"

    # ── Budget guardrails: graceful degrade (F4) ──────────────────────────
    # When a key exhausts its budget, the default behaviour is a hard 429. If
    # BUDGET_DEGRADE_ALIAS is set (e.g. "low"), the request is instead routed to
    # that cheaper alias so the caller keeps working instead of failing — the
    # response carries a {prefix}-Budget-Degraded header so clients can tell.
    budget_degrade_alias: str = ""

    # ── Server-side escalation cascade (F8) ────────────────────────────────
    # A request with model == ESCALATION_TRIGGER_MODEL walks ESCALATION_CASCADE
    # tier by tier: the gateway tries each alias in order and stops at the first
    # that returns a usable answer. Lets one request escalate by complexity /
    # on failure without the client orchestrating it.
    escalation_trigger_model: str = "auto"
    escalation_cascade: str = ""

    # ── Semantic cache (F3, optional, off by default) ──────────────────────
    # Caches chat responses keyed by an embedding of the request, so a
    # semantically similar prompt is served from cache instead of the provider.
    # Off by default — one flag turns it on. Local + ephemeral (in-process).
    semantic_cache_enabled: bool = False
    # Cosine-similarity threshold for a cache hit (0..1; higher = stricter).
    semantic_cache_threshold: float = 0.95
    # Max number of cached entries (LRU-evicted beyond this).
    semantic_cache_max_entries: int = 1000
    # Entry time-to-live in seconds (0 = no expiry).
    semantic_cache_ttl_seconds: int = 3600

    # ── Pluggable guard hooks (F6, optional) ───────────────────────────────
    # Comma-separated list of built-in or dotted-path pre/post request hooks
    # (PII redaction, content policy, EU-AI-Act tagging). Empty = none.
    # Built-in names: "pii_redact", "ai_act_tag". See app/hooks.py.
    guard_hooks: str = ""

    # ── Declarative policy file (TOML, optional, hot-reloaded) ─────────────
    # Point this at an orkoprox.toml to set routing aliases + limits + quota
    # defaults in one versionable file instead of many env vars. Empty = pure
    # env config (default). See app/policy.py for the schema.
    policy_file: str = ""

    # Provider selection. The default ships with a single OpenAI-compatible
    # upstream ("ovh"); add your own providers via env (see PROVIDERS docs).
    # `stub` is an internal test placeholder, never a real provider.
    primary_provider: str = "ovh"
    default_provider: str = "ovh"
    fallback_providers: str = "ovh"
    fallback_providers_default: str = "ovh"
    fallback_providers_xhigh: str = "ovh"
    fallback_providers_high: str = "ovh"
    fallback_providers_medium: str = "ovh"
    fallback_providers_low: str = "ovh"

    request_timeout_seconds: int = 120
    provider_max_retries: int = 2
    provider_retry_backoff_seconds: float = 0.5
    # Cap fuer exponentielles Backoff. Ohne cap waechst sleep auf
    # base * 2**attempt (bei attempts=5 + base=1s → 32s) und kann
    # uvicorn-Worker-Timeout ueberhitzen + Cascade-Latency erzeugen.
    # 5.0s ist Sweet-Spot: lang genug fuer kurze Provider-Hickups,
    # kurz genug um Request-Lifecycle nicht zu reissen.
    provider_retry_backoff_cap_seconds: float = 5.0
    # Cooldown after a provider error. In a single-provider setup there is no
    # failover target, so a long cooldown just surfaces 502s to every client
    # without benefit. 5s is the sweet spot: short enough for fast recovery,
    # long enough to avoid a retry-storm against a genuinely-down provider.
    # Rate-limit (HTTP 429) cooldown stays at 300s to protect quota budgets.
    provider_cooldown_seconds: int = 5
    provider_cooldown_seconds_ocr: int = 5
    provider_cooldown_seconds_rate_limited: int = 300

    upstream_forward_headers: str = "x-entitlements,x-quota-tier,x-org-id,x-user-id"

    # OVH AI Endpoints — default OpenAI-compatible upstream (EU data centres).
    # Set OVH_API_KEY (Bearer token) and optionally override OVH_BASE_URL.
    # Token pricing is derived from app/providers/ovh_pricing.py (pulled from
    # the upstream /v1/models endpoint).
    ovh_api_key: str = ""
    ovh_base_url: str = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"
    # Default-Modell wenn nichts spezifisch geroutet wird. Mistral-Small-3.2
    # ist Sweet-Spot: 131k ctx, Tool-Use, Visual, $0.10/$0.31 per M.
    ovh_model: str = "Mistral-Small-3.2-24B-Instruct-2506"
    # Vision-Spezialmodell: Mistral-Small-3.2 ist nativ multimodal (Default).
    # Alias-Map: "vision" → ovh_vision_model, "vision_x" → ovh_vision_premium_model.
    # Mistral-Small-3.2: $0.10/$0.31, 131k ctx, Multimodal.
    # Qwen2.5-VL-72B: $1.01/$1.01 sym., 32k ctx — a higher-cost vision model.
    ovh_vision_model: str = "Mistral-Small-3.2-24B-Instruct-2506"
    ovh_vision_premium_model: str = "Qwen2.5-VL-72B-Instruct"
    # Image-Generation: SDXL kostenlos auf OVH (W3-Pre.3, 2026-05-04).
    # Quirk: `n` Parameter NICHT supported — fuer mehrere Bilder Parallel-Calls.
    ovh_image_model: str = "stable-diffusion-xl-base-v10"
    # Embedding model: bge-multilingual-gemma2 (3584-dim, ~$0.01/M, 100+ langs).
    # 3.5x more semantic space than bge-m3 (1024-dim) at the same price.
    ovh_embedding_model: str = "bge-multilingual-gemma2"
    ovh_embedding_dimensions: int = 3584
    # Whisper: Voice-to-Text. NICHT kostenlos: OVH berechnet pro Audio-
    # Sekunde (audio_seconds-Achse, ~0.078 USD/h fuer turbo, ~0.31 USD/h
    # fuer v3 — siehe app/providers/ovh_pricing.py:OVH_AUDIO_PRICING_USD).
    ovh_whisper_model: str = "whisper-large-v3-turbo"

    # ─── Mistral La Plateforme (optional secondary provider) ───────────────
    # A second EU-hosted (Paris) OpenAI-compatible upstream, useful when you
    # want a distinct model family for premium/structured generation paths.
    # NOT a default provider — request it explicitly via the aliases below:
    #   - `report_premium`  → mistral-large-latest
    #   - `report_structure` → mistral-small-latest
    # Set MISTRAL_LP_API_KEY to enable. Pricing/weighting lives in
    # app/providers/mistral_lp_pricing.py.
    mistral_lp_api_key: str = ""
    mistral_lp_base_url: str = "https://api.mistral.ai/v1"
    mistral_lp_default_model: str = "mistral-small-latest"
    # Aliases for a premium two-stage generation path. `report_premium` = text
    # stage (Mistral-Large, creative T=0.7), `report_structure` = strict-JSON
    # stage (Mistral-Small, T=0.2). Both route through Mistral-LP for a
    # consistent style across stages. Fallback: OVH xhigh / chat respectively.
    model_alias_report_premium: str = "mistral_lp/mistral-large-latest"
    model_alias_report_structure: str = "mistral_lp/mistral-small-latest"
    fallback_providers_report_premium: str = "ovh"
    fallback_providers_report_structure: str = "ovh"

    # Tier aliases (quality-based): xhigh|high|medium|low.
    # Mistral-Small-24B is the sweet spot for tool-use + vision; Llama-3.3-70B
    # for premium quality on complex plans; Mistral-7B as the cheap-fast tier.
    model_alias_xhigh: str = "ovh/Meta-Llama-3_3-70B-Instruct"
    model_alias_high: str = "ovh/Mistral-Small-3.2-24B-Instruct-2506"
    model_alias_medium: str = "ovh/Mistral-Small-3.2-24B-Instruct-2506"
    model_alias_low: str = "ovh/Mistral-7B-Instruct-v0.3"

    # Task aliases (task-based): classify|extract|compose|chat|reason|report|ocr|vision.
    # Each task maps to the model best suited for it: small/cheap for
    # classification, larger/stronger for reasoning and report generation.
    model_alias_classify: str = "ovh/Mistral-7B-Instruct-v0.3"
    model_alias_extract: str = "ovh/Mistral-Small-3.2-24B-Instruct-2506"
    model_alias_compose: str = "ovh/Mistral-Small-3.2-24B-Instruct-2506"
    model_alias_chat: str = "ovh/Mistral-Small-3.2-24B-Instruct-2506"
    model_alias_reason: str = "ovh/gpt-oss-120b"
    model_alias_report: str = "ovh/Meta-Llama-3_3-70B-Instruct"

    # Three finer-grained reasoning tiers between `chat` (often too weak for
    # reasoning) and `reason`/`xhigh` (often oversized). Pricing + token-weights
    # for all three live in app/providers/ovh_pricing.py.
    # - reason_lite: gpt-oss-20b — small reasoning model for light logic,
    #   plan validation, tie-breaks.
    # - long_context: Qwen3.5-9B — 262k context, great for email-thread
    #   synthesis and multi-doc RAG at a price close to Mistral-Small.
    # - reason_mid: Qwen3-32B — mid-tier reasoner for plan generation and
    #   cross-checks without long-context needs, roughly half the compute cost
    #   of gpt-oss-120b.
    model_alias_reason_lite: str = "ovh/gpt-oss-20b"
    model_alias_long_context: str = "ovh/Qwen3.5-9B"
    model_alias_reason_mid: str = "ovh/Qwen3-32B"

    # Vision/OCR: Mistral-Small-3.2-24B (multimodal, 131k ctx, $0.10/$0.31).
    # Premium-Vision (vision_x): Qwen2.5-VL-72B (32k ctx, $1.01 sym., teuerstes
    # OVH-Modell — explizit anfordern, kein Default).
    model_alias_ocr: str = "ovh/Mistral-Small-3.2-24B-Instruct-2506"
    model_alias_vision: str = "ovh/Mistral-Small-3.2-24B-Instruct-2506"
    model_alias_vision_x: str = "ovh/Qwen2.5-VL-72B-Instruct"
    # Image-Generation (W3-Pre.3, 2026-05-04): SDXL kostenlos auf OVH.
    # `n`-Parameter wird abgelehnt → Konsumenten parallel callen wenn mehrere
    # Bilder gewuenscht.
    model_alias_image: str = "ovh/stable-diffusion-xl-base-v10"
    # Voice/Speech-to-Text: pay-per-second, siehe ovh_pricing.py.
    # voice = turbo (schnell, ~$0.078/h), voice_hq = v3 (HQ, langsamer, ~$0.31/h).
    model_alias_voice: str = "ovh/whisper-large-v3-turbo"
    model_alias_voice_hq: str = "ovh/whisper-large-v3"
    # Embedding alias (used by /v1/embeddings consumers and the semantic cache).
    model_alias_embed: str = "ovh/bge-multilingual-gemma2"

    # Per-task fallback chains (graceful degradation). Each ``fallback_providers_*``
    # is ENV-overridable, e.g. to add a local stub provider for dev tests.
    fallback_providers_classify: str = "ovh"
    fallback_providers_extract: str = "ovh"
    fallback_providers_compose: str = "ovh"
    fallback_providers_chat: str = "ovh"
    fallback_providers_reason: str = "ovh"
    fallback_providers_report: str = "ovh"
    fallback_providers_ocr: str = "ovh"
    fallback_providers_vision: str = "ovh"
    fallback_providers_reason_lite: str = "ovh"
    fallback_providers_long_context: str = "ovh"
    fallback_providers_reason_mid: str = "ovh"

    # ─── Content moderation guard (Qwen3Guard) ────────────────────────────
    # Pre-filter: 0.6B (p95 ~436ms), Post-filter: 8B (p95 ~759ms). Pre-guard
    # screens the request before it reaches the provider; post-guard screens the
    # response. Supports EU AI Act transparency/safety obligations (Art. 9/13/14).
    guard_enabled: bool = True
    guard_pre_model: str = "Qwen3Guard-Gen-0.6B"
    guard_post_model: str = "Qwen3Guard-Gen-8B"
    guard_pre_timeout_s: float = 2.0
    guard_post_timeout_s: float = 3.0
    # Allowlist of API keys that may skip the guard layer (e.g. internal,
    # already-PII-aware tooling). Comma-separated.
    guard_bypass_keys: str = ""
    # Allowlist of use-cases (from the X-Use-Case header) that may skip the
    # PRE-guard. Reserved for internal, non-PII classifiers where benign
    # business actions can be misread as policy violations. The POST-guard
    # still runs, so the model output stays screened. Comma-separated.
    guard_pre_skip_use_cases: str = "mode_classify,intent_classify,plan_repair"
    # Fail-open: bei Guard-Ausfall nicht blockieren, nur loggen. Pflicht
    # fuer Verfuegbarkeit. False nur in extra-strikten Compliance-Modi.
    guard_fail_open: bool = True
    # Streaming post-guard with a tail-sample. On by default: the post-guard
    # runs AFTER the stream yields, so there is no TTFB overhead — only the
    # final DONE frame is delayed. Disable via env if it causes issues at load.
    guard_post_stream_enabled: bool = True
    # Tail-Sample-Groesse fuer Streaming-Post-Guard (Bytes). 10KB ist genug
    # fuer Refusal-/Toxic-Marker am Output-Ende, schuetzt Memory bei
    # Long-Streams.
    guard_post_stream_tail_bytes: int = 10 * 1024

    # ─── Reranker sidecar (optional) ──────────────────────────────────────
    # Self-hosted cross-encoder reranking via a Docker sidecar named `reranker`.
    # Like the Whisper sidecar, it serves a model that the chat providers do
    # not offer (no /rerank endpoint upstream). Image:
    # ghcr.io/huggingface/text-embeddings-inference (TEI), serving an ONNX
    # export of the multilingual cross-encoder BAAI/bge-reranker-v2-m3. TEI
    # listens on port 80 internally and exposes POST /rerank. CPU-only.
    # Note: on CPUs without AVX-512, use an ONNX model build — TEI's Candle
    # backend segfaults there (see docker-compose.yml).
    reranker_enabled: bool = True
    reranker_base_url: str = "http://reranker:80"
    reranker_model: str = "bge-reranker-v2-m3"
    # TEI rerank is cheap + fast (CPU cross-encoder, a few ms per doc).
    # 30s covers large batches (top_n over hundreds of documents) with margin.
    reranker_timeout_s: float = 30.0

    metrics_enabled: bool = True

    # Enterprise Alerting (optional Telegram-Bot bei Backend-Ausfall)
    alert_telegram_bot_token: str = ""
    alert_telegram_chat_id: str = ""
    # Nach wie vielen konsekutiven "down"-Checks wird alarmiert.
    # 2 = verhindert Flattern bei Einmal-Hickups.
    alert_backend_down_consecutive_fails: int = 2
    # Latency-Alert: p95 in ms ueber diesem Wert fuer 5 Minuten -> Alert
    alert_p95_latency_threshold_ms: int = 30000
    # Fallback-Rate-Alert: Anteil der fallback_hits an requests > 20% fuer 10min -> Alert
    alert_fallback_rate_threshold: float = 0.2
    # Throttle: gleiche Alert-Art wird fruehestens nach X Sekunden neu versendet
    alert_throttle_seconds: int = 1800

    advertised_context_window: int = 64000
    redis_url: str = ""
    provider_capability_overrides: str = ""

    # Optional allowlist of provider names permitted when ENVIRONMENT=production.
    # Empty (default) = no restriction; any configured provider is allowed.
    # Set this (comma-separated, e.g. "ovh,mistral_lp") to hard-fail boot if the
    # primary provider is not on the list — useful to pin a deployment to
    # data-residency-compliant providers (e.g. EU-only for GDPR/EU AI Act).
    production_allowed_providers: str = ""

    @property
    def api_keys(self) -> set[str]:
        return {x.strip() for x in self.proxy_api_keys.split(",") if x.strip()}

    @property
    def admin_keys(self) -> set[str]:
        """Keys allowed on the admin plane (/v1/admin/*). Strictly separate."""
        return {x.strip() for x in self.admin_api_keys.split(",") if x.strip()}

    @property
    def escalation_cascade_tiers(self) -> list[str]:
        """Ordered tier aliases the escalation cascade walks (empty = disabled)."""
        return [x.strip() for x in self.escalation_cascade.split(",") if x.strip()]

    @property
    def guard_bypass_key_set(self) -> set[str]:
        """API-Keys die Guard-Layer ueberspringen duerfen (z.B. internal Avi)."""
        return {x.strip() for x in self.guard_bypass_keys.split(",") if x.strip()}

    @property
    def guard_pre_skip_use_case_set(self) -> set[str]:
        """Use-Cases die den Pre-Guard ueberspringen (F4)."""
        return {
            x.strip().lower()
            for x in self.guard_pre_skip_use_cases.split(",")
            if x.strip()
        }

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"prod", "production"}

    @model_validator(mode="after")
    def validate_production_api_keys(self) -> "Settings":
        if not self.is_production or not self.proxy_auth_required:
            return self
        short_keys = [
            key
            for key in self.api_keys
            if len(key) < self.production_min_api_key_length
        ]
        if short_keys:
            raise ValueError(
                "PROXY_API_KEYS contains keys shorter than "
                f"{self.production_min_api_key_length} characters for production"
            )
        return self

    @model_validator(mode="after")
    def validate_admin_plane_separation(self) -> "Settings":
        """Admin keys must be distinct from data-plane keys, and long enough
        in production. A key that is both would breach plane separation."""
        admin = self.admin_keys
        if not admin:
            return self
        overlap = admin & self.api_keys
        if overlap:
            raise ValueError(
                "ADMIN_API_KEYS must not overlap with PROXY_API_KEYS — "
                "the admin and data planes must use distinct keys."
            )
        if self.is_production:
            short = [k for k in admin if len(k) < self.production_min_api_key_length]
            if short:
                raise ValueError(
                    "ADMIN_API_KEYS contains keys shorter than "
                    f"{self.production_min_api_key_length} characters for production"
                )
        return self

    @property
    def production_allowed_provider_set(self) -> set[str]:
        return {
            x.strip().lower()
            for x in self.production_allowed_providers.split(",")
            if x.strip()
        }

    @model_validator(mode="after")
    def validate_production_primary_provider(self) -> "Settings":
        """Optionally pin the production primary provider to an allowlist.

        If ``PRODUCTION_ALLOWED_PROVIDERS`` is set and ENVIRONMENT=production,
        boot fails unless the primary provider is on the list. Empty (default)
        means no restriction — any configured provider is allowed.
        """
        if not self.is_production:
            return self
        allowed = self.production_allowed_provider_set
        if not allowed:
            return self
        primary = (self.primary_provider or self.default_provider or "").strip().lower()
        if primary and primary not in allowed:
            raise ValueError(
                f"PRIMARY_PROVIDER='{primary}' not in PRODUCTION_ALLOWED_PROVIDERS "
                f"({sorted(allowed)}) — boot blocked."
            )
        return self

    @property
    def fallback_provider_list(self) -> list[str]:
        return [x.strip() for x in self.fallback_providers.split(",") if x.strip()]

    def fallback_provider_list_for_route(self, route_key: str) -> list[str]:
        """Route-spezifische Fallback-Chain via dict-Lookup.

        ENV-Override-Prio: FALLBACK_PROVIDERS_<ROUTE> > FALLBACK_PROVIDERS.
        """
        key = (route_key or "").strip().lower()
        per_route = {
            "xhigh": self.fallback_providers_xhigh,
            "high": self.fallback_providers_high,
            "medium": self.fallback_providers_medium,
            "low": self.fallback_providers_low,
            "classify": self.fallback_providers_classify,
            "extract": self.fallback_providers_extract,
            "compose": self.fallback_providers_compose,
            "chat": self.fallback_providers_chat,
            "reason": self.fallback_providers_reason,
            "report": self.fallback_providers_report,
            "ocr": self.fallback_providers_ocr,
            "vision": self.fallback_providers_vision,
            "reason_lite": self.fallback_providers_reason_lite,
            "long_context": self.fallback_providers_long_context,
            "reason_mid": self.fallback_providers_reason_mid,
            "report_premium": self.fallback_providers_report_premium,
            "report_structure": self.fallback_providers_report_structure,
            "default": self.fallback_providers_default,
        }
        value = per_route.get(key) or self.fallback_providers_default or self.fallback_providers
        route_specific = [x.strip() for x in value.split(",") if x.strip()]
        merged: list[str] = []
        for provider in route_specific + self.fallback_provider_list:
            if provider not in merged:
                merged.append(provider)
        return merged

    @property
    def forward_headers(self) -> set[str]:
        return {
            x.strip().lower()
            for x in self.upstream_forward_headers.split(",")
            if x.strip()
        }

    @property
    def effective_primary_provider(self) -> str:
        return self.primary_provider or self.default_provider

    @property
    def provider_capability_overrides_map(self) -> dict[str, ProviderCapabilities]:
        return parse_capability_overrides(self.provider_capability_overrides)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
