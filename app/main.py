from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)

from app.config import get_settings
from app.errors import ProxyError
from app.logging_utils import (
    _provider_response_excerpt,
    _provider_status_of,
    configure_logging,
    log_event,
)
from app.metrics import MetricsStore
from app.providers.base import ProviderError, ProviderRequestContext
from app.providers.router import OPENAI_COMPAT_MODEL_ALIASES, ProviderRegistry
from app.services.content_moderation import (
    SAFE_FALLBACK_TEXT,
    GuardDecision,
    check_safety_post,
    check_safety_pre,
)
from app.token_metering import TokenMeteringService, KeyConfig
from app.schemas import ChatCompletionsRequest, EmbeddingsRequest, RerankRequest
from app.admin_auth import enforce_admin_auth, matches_any
from app.audit_log import AuditLog, mask_key
from app.rate_limit import RateLimiter
from app.policy import PolicyLoader, apply_policy_to_settings
from app.semantic_cache import SemanticCache
from app.hooks import GuardHook, load_hooks, run_post_hooks, run_pre_hooks
from app.compat import (
    anthropic_to_openai,
    ollama_to_openai,
    openai_to_anthropic,
    openai_to_ollama,
)


settings = get_settings()

# Declarative policy (optional TOML): merge alias/limit overrides into settings
# BEFORE the router reads them. Hot-reload of limits/quotas is read live by the
# consuming components; routing-alias changes apply on the next reload.
policy_loader = PolicyLoader(settings.policy_file or None)
if policy_loader.enabled:
    apply_policy_to_settings(policy_loader.policy, settings)

# Namespace for gateway-emitted response headers (e.g. "X-Orkoprox-Guard-Blocked").
# Configurable via BRAND_HEADER_PREFIX so deployments can rebrand without code edits.
_HP = settings.brand_header_prefix.rstrip("-")
logger = configure_logging(settings.log_level, settings.log_json, settings.log_file)
metrics_store = MetricsStore()
provider_registry = ProviderRegistry(settings)

_metering_redis = None
if settings.redis_url:
    import redis as _redis_lib

    _metering_redis = _redis_lib.Redis.from_url(settings.redis_url, decode_responses=False)
metering_service = TokenMeteringService(
    redis_client=_metering_redis, header_prefix=settings.brand_header_prefix
)

# Per-key request rate / concurrency limiting (opt-in; 0 = disabled).
rate_limiter = RateLimiter(
    per_minute=settings.rate_limit_per_minute,
    burst=settings.rate_limit_burst,
    concurrency=settings.rate_limit_concurrency,
)

# Append-only audit log (opt-in). Never records full keys or prompt content.
audit_log = AuditLog(enabled=settings.audit_log_enabled, path=settings.audit_log_path)

# Semantic response cache (F3, opt-in). Local, in-process, off by default.
semantic_cache = SemanticCache(
    enabled=settings.semantic_cache_enabled,
    threshold=settings.semantic_cache_threshold,
    max_entries=settings.semantic_cache_max_entries,
    ttl_seconds=settings.semantic_cache_ttl_seconds,
)

# Pluggable guard hooks (F6, opt-in). Off by default (empty list).
# Activated via GUARD_HOOKS env var, e.g. GUARD_HOOKS=pii_redact,ai_act_tag.
guard_hooks: list[GuardHook] = load_hooks(settings.guard_hooks)


async def _embed_for_cache(text: str, ctx: Any) -> list[float]:
    """Embed text for semantic-cache keying. Returns [] on any failure (the
    cache then simply misses — it must never break a request)."""
    if not text:
        return []
    try:
        _, _, data, _ = await provider_registry.embeddings(
            settings.model_alias_embed, {"input": text}, ctx
        )
        items = data.get("data") or []
        if items and isinstance(items[0], dict):
            emb = items[0].get("embedding")
            if isinstance(emb, list):
                return [float(x) for x in emb]
    except Exception:  # noqa: BLE001 — cache embedding is best-effort
        pass
    return []


def _enforce_rate_limit(api_key: str | None) -> None:
    """Reject with 429 if the key exceeds its request-rate budget."""
    key = api_key or "anonymous"
    if not rate_limiter.check_rate(key):
        raise ProxyError(
            http_status=429,
            code="rate_limit_exceeded",
            message="request rate limit exceeded — slow down",
        )


_healthz_redis = None
if settings.redis_url:
    try:
        import redis.asyncio as _aioredis

        _healthz_redis = _aioredis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        _healthz_redis = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    yield
    # Graceful shutdown: close Redis connections
    if _metering_redis is not None:
        _metering_redis.close()
    if _healthz_redis is not None:
        try:
            await _healthz_redis.aclose()
        except Exception:
            pass


app = FastAPI(title="orkoprox", version="0.1.1", lifespan=lifespan)


def _error_response(request_id: str, err: ProxyError) -> JSONResponse:
    return JSONResponse(
        status_code=err.http_status, content=jsonable_encoder(err.as_body(request_id))
    )


def _normalize_provider_error(err: ProviderError) -> ProxyError:
    return ProxyError(
        http_status=502 if err.status_code >= 500 else err.status_code,
        code=err.code,
        message=str(err),
        details=err.details or None,
    )


def _sanitize_tool_calls(data: dict[str, Any]) -> dict[str, Any]:
    """Sanitize tool_call arguments in provider responses.

    Some providers (gpt-oss-120b, GLM-4.6) return tool_calls with empty
    or invalid JSON arguments. If these are echoed back in multi-turn
    conversations, ALL providers reject with "invalid tool call arguments"
    → 502 cascade across the entire fallback chain.

    Fix: replace empty/invalid arguments with "{}".
    """
    choices = data.get("choices")
    if not isinstance(choices, list):
        return data
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments", "")
            if not args or not isinstance(args, str) or not args.strip():
                fn["arguments"] = "{}"
            else:
                try:
                    json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    fn["arguments"] = "{}"
    return data


def _extract_api_key(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _authenticate_data_key(
    x_api_key: str | None, authorization: str | None
) -> str | None:
    """Validate a data-plane API key and return it. Returns None if auth is disabled."""
    allowed = settings.api_keys
    if not settings.proxy_auth_required:
        return None
    if not allowed:
        # Also check metered keys
        presented_key = _extract_api_key(x_api_key, authorization)
        if presented_key and metering_service.enabled:
            config = metering_service.get_key_config(presented_key)
            if config and config.active:
                return presented_key
        raise ProxyError(
            http_status=503,
            code="auth_misconfigured",
            message="proxy auth is required but no PROXY_API_KEYS are configured",
        )
    presented_key = _extract_api_key(x_api_key, authorization)
    if not presented_key:
        raise ProxyError(http_status=401, code="missing_api_key", message="missing_api_key")
    # Check static keys first (constant-time, avoids timing-leaking the key),
    # then metered keys.
    if not matches_any(presented_key, allowed):
        if metering_service.enabled:
            config = metering_service.get_key_config(presented_key)
            if config and config.active:
                return presented_key
        raise ProxyError(http_status=401, code="invalid_api_key", message="invalid_api_key")
    return presented_key


def _enforce_internal_auth(x_api_key: str | None, authorization: str | None) -> str | None:
    """Authenticate a data-plane request and enforce its request-rate budget.

    Returns the API key (for metering) or None if auth is disabled. Rate
    limiting runs after successful auth so unauthenticated traffic can't be used
    to probe limits.
    """
    auth_key = _authenticate_data_key(x_api_key, authorization)
    _enforce_rate_limit(auth_key)
    return auth_key


def _enforce_metering_budget(api_key: str | None) -> None:
    """Check token budget for metered keys. Raises 429 if exceeded.

    Hard-stop variant used by endpoints without a degrade path (embeddings,
    rerank, audio, image, vision).
    """
    if not api_key or not metering_service.enabled:
        return
    allowed, config, usage = metering_service.check_budget(api_key)
    if not allowed:
        if config and not config.active:
            raise ProxyError(
                http_status=403,
                code="key_revoked",
                message="API key has been revoked",
            )
        limit = config.daily_token_limit if config else 0
        raise ProxyError(
            http_status=429,
            code="token_budget_exceeded",
            message=f"Daily token budget exceeded ({usage.total_tokens}/{limit}). Resets at midnight UTC.",
        )


def _budget_degrade_or_enforce(api_key: str | None) -> str | None:
    """Budget guardrail with optional graceful degrade (F4).

    Returns a model alias to downgrade to when the key's budget is exhausted and
    BUDGET_DEGRADE_ALIAS is configured; returns None when the budget is fine or
    metering is off. Raises 429 (or 403 for a revoked key) when the budget is
    exhausted and no degrade alias is set — the existing hard-stop behaviour.
    """
    if not api_key or not metering_service.enabled:
        return None
    allowed, config, usage = metering_service.check_budget(api_key)
    if allowed:
        return None
    if config and not config.active:
        raise ProxyError(
            http_status=403, code="key_revoked", message="API key has been revoked"
        )
    degrade = settings.budget_degrade_alias.strip()
    if degrade:
        return degrade
    limit = config.daily_token_limit if config else 0
    raise ProxyError(
        http_status=429,
        code="token_budget_exceeded",
        message=f"Daily token budget exceeded ({usage.total_tokens}/{limit}). Resets at midnight UTC.",
    )


def _extract_usage_from_response(data: dict[str, Any]) -> tuple[int, int]:
    """Extract token counts from OpenAI-compatible response."""
    usage = data.get("usage", {})
    return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


async def _aclose_quietly(stream: Any) -> None:
    """Best-effort close of an async stream/response. Never raises.

    Prevents an httpx AsyncClient leak when a streamed request is cancelled or
    the client disconnects.
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        result = aclose()
        if result is not None:
            await result
    except Exception:  # noqa: BLE001 — cleanup must never propagate
        pass


def _is_empty_chat_response(data: dict[str, Any]) -> bool:
    """True if a chat completion carries no usable assistant content.

    Used by the escalation cascade to decide whether to try the next tier.
    A response with tool calls counts as usable even when content is empty.
    """
    try:
        choices = data.get("choices") or []
        if not choices:
            return True
        message = choices[0].get("message") or {}
        if message.get("tool_calls"):
            return False
        content = message.get("content")
        return not (content and str(content).strip())
    except (AttributeError, IndexError, TypeError):
        return True


def _track_provider_usage(
    provider_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    api_key: str | None,
) -> None:
    """Record per-provider token usage and per-key request counter."""
    if prompt_tokens > 0 or completion_tokens > 0:
        metrics_store.record_provider_token_usage(provider_name, prompt_tokens, completion_tokens)
    metrics_store.record_per_key_request(api_key)


def _record_fallback_if_needed(selected_provider: str, route_debug: Any) -> None:
    """Wenn der tatsaechlich gewaehlte Provider nicht der primaer geresolvte ist,
    war es ein Fallback-Hit. Metriken zaehlen (resolved_provider -> selected_provider).
    route_debug ist ein dict mit 'resolved_provider' und 'fallback_chain'.
    """
    if not isinstance(route_debug, dict):
        return
    resolved = route_debug.get("resolved_provider")
    if not resolved or resolved == selected_provider:
        return
    metrics_store.record_fallback_hit(resolved, selected_provider)


def _add_metering_headers(
    response: JSONResponse, api_key: str | None, usage_after: Any | None = None
) -> JSONResponse:
    """Add metering headers to response for client-side display."""
    if not api_key or not metering_service.enabled:
        return response
    config = metering_service.get_key_config(api_key)
    if config is None:
        return response
    daily_usage = usage_after or metering_service.get_daily_usage(api_key)
    headers = metering_service.build_usage_headers(config, daily_usage, api_key=api_key)
    for k, v in headers.items():
        response.headers[k] = v
    return response


def _build_models_payload() -> dict[str, Any]:
    alias_targets = {
        # Tier aliases (quality-based)
        "xhigh": settings.model_alias_xhigh,
        "high": settings.model_alias_high,
        "medium": settings.model_alias_medium,
        "low": settings.model_alias_low,
        # Task aliases (task-based — proxy picks optimal model)
        "classify": settings.model_alias_classify,
        "extract": settings.model_alias_extract,
        "compose": settings.model_alias_compose,
        "chat": settings.model_alias_chat,
        "reason": settings.model_alias_reason,
        "report": settings.model_alias_report,
        "ocr": settings.model_alias_ocr,
        "vision": settings.model_alias_vision,
    }
    reasoning_levels = [
        {"effort": "low", "description": "Fast responses with lighter reasoning"},
        {
            "effort": "medium",
            "description": "Balances speed and reasoning depth for everyday tasks",
        },
        {
            "effort": "high",
            "description": "Greater reasoning depth for complex problems",
        },
    ]
    data = [
        {
            "id": alias,
            "object": "model",
            "created": 0,
            "owned_by": "orkoprox",
            "permission": [],
            "root": target,
            "parent": None,
            "display_name": alias,
            "description": f"orkoprox tier alias for {target}",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": reasoning_levels,
            "supports_parallel_tool_calls": True,
            "supports_reasoning_summaries": True,
            "default_reasoning_summary": "none",
            "support_verbosity": True,
            "default_verbosity": "low",
            "context_window": settings.advertised_context_window,
            "visibility": "list",
            "supported_in_api": True,
        }
        for alias, target in alias_targets.items()
        if target.strip()
    ]
    seen = {entry["id"] for entry in data}
    for target in alias_targets.values():
        normalized = target.strip()
        if not normalized or normalized in seen:
            continue
        data.append(
            {
                "id": normalized,
                "object": "model",
                "created": 0,
                "owned_by": "orkoprox",
                "permission": [],
                "root": normalized,
                "parent": None,
                "display_name": normalized,
                "description": "orkoprox resolved upstream model",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": reasoning_levels,
                "supports_parallel_tool_calls": True,
                "supports_reasoning_summaries": True,
                "default_reasoning_summary": "none",
                "support_verbosity": True,
                "default_verbosity": "low",
                "context_window": settings.advertised_context_window,
                "visibility": "list",
                "supported_in_api": True,
            }
        )
        seen.add(normalized)
    for alias_id, alias_route in OPENAI_COMPAT_MODEL_ALIASES.items():
        if alias_id in seen:
            continue
        target = alias_targets.get(alias_route, alias_route)
        data.append(
            {
                "id": alias_id,
                "object": "model",
                "created": 0,
                "owned_by": "orkoprox",
                "permission": [],
                "root": target,
                "parent": alias_route,
                "display_name": alias_id,
                "description": f"orkoprox OpenAI-compatible alias for {alias_route}",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": reasoning_levels,
                "supports_parallel_tool_calls": True,
                "supports_reasoning_summaries": True,
                "default_reasoning_summary": "none",
                "support_verbosity": True,
                "default_verbosity": "low",
                "context_window": settings.advertised_context_window,
                "visibility": "list",
                "supported_in_api": True,
            }
        )
        seen.add(alias_id)
    return {"object": "list", "data": data}


def _extract_forward_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in request.headers.items():
        lowered = key.lower()
        if lowered in settings.forward_headers:
            out[lowered] = value
    return out


def _make_request_context(request: Request) -> ProviderRequestContext:
    request_id = str(request.state.request_id)
    health_probe = request.headers.get("x-health-probe", "").strip() == "1"
    return ProviderRequestContext(
        request_id=request_id,
        forward_headers=_extract_forward_headers(request),
        is_health_probe=health_probe,
    )


def _sse_event(data: Any) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=True)}\n\n".encode("utf-8")


async def _iter_sse_data(stream: AsyncIterator[bytes]) -> AsyncIterator[str]:
    buffer = ""
    async for chunk in stream:
        text = chunk.decode("utf-8", errors="ignore")
        buffer += text
        while "\n\n" in buffer:
            raw_event, buffer = buffer.split("\n\n", 1)
            for line in raw_event.splitlines():
                if line.startswith("data:"):
                    yield line[5:].strip()
    if buffer.strip():
        for line in buffer.splitlines():
            if line.startswith("data:"):
                yield line[5:].strip()


def _extract_sse_data_lines(buffer: str, chunk_text: str) -> tuple[list[str], str]:
    collected: list[str] = []
    buffer += chunk_text
    while "\n\n" in buffer:
        raw_event, buffer = buffer.split("\n\n", 1)
        for line in raw_event.splitlines():
            if line.startswith("data:"):
                collected.append(line[5:].strip())
    return collected, buffer


def _extract_streaming_chunk_text(data_line: str) -> str:
    """Extrahiert den content-String aus einer SSE-data-Zeile eines OpenAI-
    Streaming-Chunks. Returnt leeren String wenn data_line kein gueltiger
    Chunk ist (z.B. ``[DONE]`` oder kaputtes JSON).

    Format-Erwartung:
        {"choices":[{"delta":{"content":"..."}}], ...}

    Collected by stream_iter() into the tail buffer for the streaming post-guard.
    """
    if not data_line or data_line == "[DONE]":
        return ""
    try:
        chunk = json.loads(data_line)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(chunk, dict):
        return ""
    choices = chunk.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta") or {}
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _coerce_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            if item.strip():
                parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
            continue
        value = item.get("content")
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts).strip()


def _apply_tooling_guidance(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    if not tools:
        return messages
    tool_names: list[str] = [
        str(tool["name"])
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    ]
    if not tool_names:
        return messages
    guidance = (
        "If you decide to call a tool, you must use one of these exact tool names only: "
        + ", ".join(tool_names)
        + ". Do not invent or rename tools."
    )
    updated = [dict(message) for message in messages]
    if (
        updated
        and updated[0].get("role") == "system"
        and isinstance(updated[0].get("content"), str)
    ):
        updated[0]["content"] = f"{updated[0]['content']}\n\n{guidance}"
        return updated
    return [{"role": "system", "content": guidance}, *updated]


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid4())
    request.state.request_id = request_id
    request.state.started_at = time.perf_counter()

    # Reject oversized bodies up front (cheap Content-Length check) so a single
    # large request cannot exhaust memory before reaching the upstream call.
    max_body = settings.max_request_body_bytes
    if max_body > 0:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared > max_body:
                return _error_response(
                    request_id,
                    ProxyError(
                        http_status=413,
                        code="request_too_large",
                        message=f"request body exceeds {max_body} bytes",
                    ),
                )

    try:
        response = await call_next(request)
    except ProxyError as err:
        response = _error_response(request_id, err)

    elapsed_ms = (time.perf_counter() - request.state.started_at) * 1000
    response.headers["x-request-id"] = request_id
    response.headers["x-proxy-latency-ms"] = str(int(elapsed_ms))
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    req_id = getattr(request.state, "request_id", str(uuid4()))
    log_event(
        logger,
        "validation_error",
        request_id=req_id,
        path=str(request.url.path),
        errors=str(exc.errors())[:500],
    )
    err = ProxyError(
        http_status=422,
        code="validation_error",
        message="invalid request payload",
        details=jsonable_encoder(exc.errors()),
    )
    return _error_response(req_id, err)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, "request_id", str(uuid4()))
    err = ProxyError(
        http_status=exc.status_code,
        code="http_error",
        message=str(exc.detail),
    )
    return _error_response(req_id, err)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions — log full traceback, return 500."""
    import logging
    import traceback

    req_id = getattr(request.state, "request_id", str(uuid4()))
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logging.getLogger("app.main").error(
        "Unhandled exception on %s %s: %s\n%s",
        request.method,
        request.url.path,
        exc,
        "".join(tb),
    )
    log_event(
        logger,
        "unhandled_exception",
        request_id=req_id,
        path=str(request.url.path),
        method=request.method,
        error=str(exc)[:500],
    )
    err = ProxyError(
        http_status=500,
        code="internal_error",
        message="Internal proxy error",
        details={"exception": str(exc)[:200]},
    )
    return _error_response(req_id, err)


@app.get("/health")
@app.get("/health/live")
async def health() -> dict[str, str]:
    """Liveness-Probe (K8s/Traefik): nur "Proxy-Prozess lebt".

    Aliased ueber /health (legacy) und /health/live (K8s-Konvention).
    Macht KEINE Upstream-Probe — fuer "ist OVH erreichbar?" → /health/ready
    oder /v1/healthz.
    """
    return {"status": "ok"}


@app.get("/ready")
@app.get("/health/ready")
async def ready() -> dict[str, str]:
    """Readiness-Probe: Proxy ist konfiguriert + hat einen aktiven Provider.

    Aliased ueber /ready (legacy) und /health/ready (K8s-Konvention).
    Schnell + ohne Network-Probe — fuer eine echte OVH-Verfuegbarkeits-
    Pruefung mit Latenz-Messung greift /v1/healthz (auth-required, cached).
    """
    if settings.proxy_auth_required and not settings.api_keys:
        raise ProxyError(
            http_status=503,
            code="not_ready",
            message="auth required but PROXY_API_KEYS missing",
        )
    if provider_registry.provider_ready():
        return {"status": "ready"}
    raise ProxyError(http_status=503, code="not_ready", message="no configured provider available")


@app.get("/v1/healthz")
async def healthz(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    no_cache: bool = Query(default=False),
) -> JSONResponse:
    """Deep health probe: checks all backends in parallel, cached ~10s.

    Consumers that just want to know whether the proxy is alive should hit this
    endpoint instead of burning real tokens against /v1/chat/completions.

    - 200: overall status "ok" (all required backends reachable)
    - 200 + status "degraded": a non-required backend is down, proxy still serves
    - 503 + status "critical": all required backends down, proxy is blind

    Auth like /v1/models: health probes are gated so no free backend
    reconnaissance is possible.
    """
    _enforce_internal_auth(x_api_key, authorization)
    from app.healthz import get_healthz, probe_all_backends

    redis_for_cache = None if no_cache else _healthz_redis
    if no_cache:
        result = await probe_all_backends(settings)
        result["cache"] = "bypass"
    else:
        result = await get_healthz(settings, redis_for_cache)

    # Metrics: pro Backend Up/Down-Zustand exponieren
    for backend in result.get("backends", []):
        metrics_store.set_backend_up(
            backend["name"],
            1 if backend["status"] == "ok" else 0,
        )

    http_status = 200 if result["status"] != "critical" else 503
    return JSONResponse(result, status_code=http_status)


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    if not settings.metrics_enabled:
        raise ProxyError(
            http_status=404,
            code="metrics_disabled",
            message="metrics endpoint disabled",
        )
    return PlainTextResponse(
        metrics_store.render_prometheus(), media_type="text/plain; version=0.0.4"
    )


@app.get("/metrics/provider-usage")
async def metrics_provider_usage(
    date: str | None = Query(default=None),
) -> JSONResponse:
    """Per-provider daily token usage for quota monitoring.

    Returns token counts per provider for the given date (default: today).
    """
    usage = metrics_store.get_provider_token_usage(date)
    return JSONResponse(
        {
            "date": date or datetime.now(UTC).strftime("%Y-%m-%d"),
            "providers": usage,
        }
    )


@app.get("/v1/models")
async def models(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _enforce_internal_auth(x_api_key, authorization)
    return JSONResponse(_build_models_payload())


@app.post("/v1/embeddings")
async def embeddings(
    request: Request,
    body: EmbeddingsRequest,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    auth_key = _enforce_internal_auth(x_api_key, authorization)
    _enforce_metering_budget(auth_key)

    payload: dict[str, Any] = body.model_dump(by_alias=True, exclude_none=True)
    ctx = _make_request_context(request)
    started = time.perf_counter()

    try:
        provider_name, resolved_model, data, route_debug = await provider_registry.embeddings(
            body.model,
            payload,
            ctx,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics_store.record(provider_name, resolved_model, 200, False, elapsed_ms)

        prompt_t, completion_t = _extract_usage_from_response(data)
        _track_provider_usage(provider_name, prompt_t, completion_t, auth_key)

        usage_after = None
        if auth_key and metering_service.enabled:
            if prompt_t > 0 or completion_t > 0:
                usage_after = metering_service.record_usage(
                    auth_key,
                    prompt_tokens=prompt_t,
                    completion_tokens=completion_t,
                    model=resolved_model,
                    provider=provider_name,
                )

        log_event(
            logger,
            "request_completed",
            request_id=ctx.request_id,
            path="/v1/embeddings",
            provider=provider_name,
            raw_model=body.model,
            model=resolved_model,
            latency_ms=int(elapsed_ms),
            status_code=200,
            stream=False,
            route_debug=route_debug,
        )
        resp = JSONResponse(_sanitize_tool_calls(data))
        return _add_metering_headers(resp, auth_key, usage_after)
    except ProviderError as exc:
        err = _normalize_provider_error(exc)
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics_store.record("none", body.model, err.http_status, False, elapsed_ms)
        log_event(
            logger,
            "request_failed",
            request_id=ctx.request_id,
            path="/v1/embeddings",
            provider="none",
            model=body.model,
            latency_ms=int(elapsed_ms),
            status_code=err.http_status,
            stream=False,
            code=err.code,
            message=err.message,
            provider_status=_provider_status_of(exc),
            provider_response=_provider_response_excerpt(exc),
        )
        raise err


@app.post("/v1/rerank")
async def rerank(
    request: Request,
    body: RerankRequest,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Cross-Encoder-Reranking via self-hosted TEI-Sidecar.

    Cohere-/Jina-kompatibler Endpoint. Reranking nutzt das
    `bge-reranker-v2-m3`-Modell im `reranker`-Docker-Sidecar — OVH und
    Mistral bieten KEINEN Rerank-Endpoint (verifiziert: 404). Analogie zum
    Whisper-Sidecar fuer ASR.

    Request (Cohere-/Jina-Spec):
      {
        "model": "bge-reranker-v2-m3" oder "rerank" (egal — es gibt nur
                  einen Reranker),
        "query": "...",            (Pflicht)
        "documents": ["...", ...], (Pflicht)
        "top_n": 5,                (optional — Ergebnis abschneiden)
        "return_documents": false  (optional — Originaltext anhaengen)
      }
    Response:
      {
        "model": "...",
        "results": [{"index": int, "relevance_score": float,
                     "document": {"text": "..."}?}, ...],
        "usage": {"prompt_tokens": int, "total_tokens": int}
      }
    """
    auth_key = _enforce_internal_auth(x_api_key, authorization)
    _enforce_metering_budget(auth_key)

    payload: dict[str, Any] = body.model_dump(exclude_none=True)
    ctx = _make_request_context(request)
    started = time.perf_counter()

    try:
        provider_name, resolved_model, data, route_debug = await provider_registry.rerank(
            body.model,
            payload,
            ctx,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics_store.record(provider_name, resolved_model, 200, False, elapsed_ms)

        prompt_t, completion_t = _extract_usage_from_response(data)
        _track_provider_usage(provider_name, prompt_t, completion_t, auth_key)

        usage_after = None
        if auth_key and metering_service.enabled:
            if prompt_t > 0 or completion_t > 0:
                usage_after = metering_service.record_usage(
                    auth_key,
                    prompt_tokens=prompt_t,
                    completion_tokens=completion_t,
                    model=resolved_model,
                    provider=provider_name,
                )

        log_event(
            logger,
            "request_completed",
            request_id=ctx.request_id,
            path="/v1/rerank",
            provider=provider_name,
            raw_model=body.model,
            model=resolved_model,
            latency_ms=int(elapsed_ms),
            status_code=200,
            stream=False,
            route_debug=route_debug,
        )
        resp = JSONResponse(data)
        return _add_metering_headers(resp, auth_key, usage_after)
    except ProviderError as exc:
        # Reranking hat nur EINEN Provider (das Sidecar) — kein Fallback.
        # Status-Code wird 1:1 durchgereicht (NICHT ueber
        # _normalize_provider_error, das 5xx auf 502 klemmt): so bleiben
        # 503 reranker_unavailable / 504 reranker_timeout fuer Konsumenten
        # eindeutig diagnostizierbar. Analog zum Whisper-Endpoint.
        err = ProxyError(
            http_status=exc.status_code,
            code=exc.code,
            message=str(exc),
            details=exc.details or None,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics_store.record("none", body.model, err.http_status, False, elapsed_ms)
        log_event(
            logger,
            "request_failed",
            request_id=ctx.request_id,
            path="/v1/rerank",
            provider="none",
            model=body.model,
            latency_ms=int(elapsed_ms),
            status_code=err.http_status,
            stream=False,
            code=err.code,
            message=err.message,
            provider_status=_provider_status_of(exc),
            provider_response=_provider_response_excerpt(exc),
        )
        raise err


def _extract_last_user_text(messages: list[Any]) -> str:
    """Letzte User-Message als Text (fuer Guard-Pre-Filter).

    Multimodal: nimmt nur text-content-parts, ignoriert image_url etc.
    """
    for msg in reversed(messages):
        role = msg.role if hasattr(msg, "role") else msg.get("role")
        if role != "user":
            continue
        content = msg.content if hasattr(msg, "content") else msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    parts.append(part)
            return " ".join(parts).strip()
        return str(content or "")
    return ""


def _should_skip_guard(auth_key: str | None, request: Request) -> bool:
    """Pre-Guard-Skip in zwei Modi:

    1. Manueller Bypass via X-Skip-Content-Guard:true Header — nur wenn der
       Auth-Key in guard_bypass_keys-Whitelist ist (Tenant-Level-Trust).

    2. Use-Case-basierter Bypass via X-Use-Case-Header — nur wenn der
       Use-Case in guard_pre_skip_use_cases-Whitelist ist (server-side
       definiert, kein Auth-Key-Check noetig). Reserviert fuer interne
       Klassifikatoren (mode_classify, intent_classify, plan_repair) wo
       legale Geschaefts-Aktionen vom Pre-Guard missinterpretiert wuerden.
       Reserved for internal classifiers (mode_classify, intent_classify, plan_repair).

    Beide Modi skippen NUR den Pre-Guard. Post-Guard laeuft normal — der
    LLM-Output bleibt also stets gefiltert.
    """
    if not settings.guard_enabled:
        return True

    # Modus 1: manueller Header-Bypass mit Auth-Key-Whitelist
    if request.headers.get("x-skip-content-guard", "").lower() == "true":
        if auth_key and auth_key in settings.guard_bypass_key_set:
            return True

    # Modus 2: Use-Case-basierter Bypass (F4)
    use_case = request.headers.get("x-use-case", "").strip().lower()
    if use_case and use_case in settings.guard_pre_skip_use_case_set:
        return True

    return False


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionsRequest,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
):
    auth_key = _enforce_internal_auth(x_api_key, authorization)
    _budget_degrade_alias = _budget_degrade_or_enforce(auth_key)

    payload: dict[str, Any] = body.model_dump(by_alias=True, exclude_none=True)
    # F4 graceful degrade: budget exhausted + a degrade alias is configured →
    # route this request to the cheaper alias instead of returning 429.
    if _budget_degrade_alias:
        payload["model"] = _budget_degrade_alias
    # F8 escalation cascade: a request addressed to the trigger model walks the
    # configured tier list, stopping at the first usable answer.
    _cascade_tiers: list[str] = []
    if (
        not _budget_degrade_alias
        and payload.get("model") == settings.escalation_trigger_model
        and settings.escalation_cascade_tiers
    ):
        _cascade_tiers = settings.escalation_cascade_tiers
        payload["model"] = _cascade_tiers[0]
    # The model actually routed (may differ from body.model after F4/F8 above).
    effective_model = payload.get("model", body.model)
    # Messages need special serialization: assistant messages with tool_calls
    # MUST include content=null (OpenAI spec), so exclude_none would break them.
    serialized_messages = []
    for message in body.messages:
        msg = message.model_dump(by_alias=True, exclude_none=True)
        if message.role == "assistant" and message.tool_calls and "content" not in msg:
            msg["content"] = None
        serialized_messages.append(msg)
    payload["messages"] = serialized_messages
    if body.tools:
        payload["messages"] = _apply_tooling_guidance(payload["messages"], body.tools)
    for key in ("tools", "tool_choice", "parallel_tool_calls", "response_format"):
        value = getattr(body, key, None)
        if value is not None:
            payload[key] = value
    ctx = _make_request_context(request)
    started = time.perf_counter()

    # ─── Semantic cache (F3) ─────────────────────────────────────────────────
    # Only cache deterministic, non-streaming, non-tool requests. A hit returns
    # the cached response immediately (no provider call, no guard re-run since
    # the cached answer was already guarded when first produced).
    _cache_embedding: list[float] = []
    _cacheable = (
        semantic_cache.enabled
        and not body.stream
        and not body.tools
        and not _cascade_tiers
    )
    if _cacheable:
        _cache_text = _extract_last_user_text(body.messages)
        _cache_embedding = await _embed_for_cache(_cache_text, ctx)
        cached = semantic_cache.lookup(_cache_embedding)
        if cached is not None:
            metrics_store.record("cache", effective_model, 200, False, 0.0)
            log_event(
                logger,
                "request_completed",
                request_id=ctx.request_id,
                path="/v1/chat/completions",
                provider="cache",
                raw_model=body.model,
                model=effective_model,
                latency_ms=0,
                status_code=200,
                stream=False,
                cache="hit",
            )
            resp = JSONResponse(_sanitize_tool_calls(cached))
            resp.headers[f"{_HP}-Cache"] = "hit"
            return _add_metering_headers(resp, auth_key, None)

    # ─── Qwen3Guard pre-filter ───────────────────────────────────────────────
    # Pre-guard runs PARALLEL to the provider connect instead of sequentially
    # before it. At typical OVH latencies (~600-1000ms) and pre-guard latency
    # (~300-500ms) this reduces wall-time from sum() to max(provider, guard)
    # — typically 300-500ms saved per call.
    #
    # On pre-block: provider_task.cancel() + try/except, then HTTP 451.
    # await pre_task is enforced before any output yield → no output leak
    # before pre-decision (fail-closed on race condition).

    # ─── F6: pluggable pre-hooks (PII redaction, policy tagging …) ──────────
    # Runs before everything else so that PII never reaches the provider or
    # the Qwen3Guard (which also logs).  Hooks operate on the serialised
    # payload["messages"] directly so the transformed text flows through the
    # entire request pipeline unchanged.
    _hook_pre_tags: dict[str, str] = {}
    if guard_hooks:
        _hook_input = _extract_last_user_text(body.messages)
        _hook_ctx: dict = {
            "model": effective_model,
            "request_id": ctx.request_id,
        }
        _hook_text, _hook_pre_tags, _hook_blocked, _hook_block_reason = run_pre_hooks(
            guard_hooks, _hook_input, _hook_ctx
        )
        if _hook_blocked:
            return JSONResponse(
                status_code=451,
                content={
                    "error": "Request blocked by guard hook",
                    "code": "hook_blocked",
                    "reason": _hook_block_reason,
                    "request_id": ctx.request_id,
                },
            )
        # If the text was transformed (e.g. PII stripped), patch the last user
        # message in the serialised payload so the provider receives clean text.
        if _hook_text != _hook_input:
            for i in range(len(payload["messages"]) - 1, -1, -1):
                if payload["messages"][i].get("role") == "user":
                    payload["messages"][i] = dict(payload["messages"][i])
                    payload["messages"][i]["content"] = _hook_text
                    break

    skip_guard = _should_skip_guard(auth_key, request)
    user_input_text = _extract_last_user_text(body.messages) if not skip_guard else ""
    pre_decision: GuardDecision | None = None
    pre_task: asyncio.Task[GuardDecision] | None = None
    if not skip_guard and user_input_text:
        pre_task = asyncio.create_task(
            check_safety_pre(
                user_input_text,
                base_url=settings.ovh_base_url,
                api_key=settings.ovh_api_key,
                model=settings.guard_pre_model,
                timeout_s=settings.guard_pre_timeout_s,
                fail_open=settings.guard_fail_open,
            )
        )

    async def _await_pre_decision() -> GuardDecision | None:
        """Awaitet das Pre-Guard-Task. Returnt None wenn kein Task lief."""
        if pre_task is None:
            return None
        return await pre_task

    def _build_pre_block_response(decision: GuardDecision) -> JSONResponse:
        log_event(
            logger,
            "guard_blocked_pre",
            request_id=ctx.request_id,
            path="/v1/chat/completions",
            guard_safety=decision.safety,
            guard_categories=list(decision.categories),
            guard_reason=decision.reason,
            guard_latency_ms=int(decision.latency_ms),
            guard_model=decision.model,
        )
        log_event(
            logger,
            "guard_audit",
            request_id=ctx.request_id,
            path="/v1/chat/completions",
            guard_pre=decision.to_audit_dict(),
            guard_post=None,
            final_allowed=False,
            final_blocked_reason="pre",
            guard_pre_parallel=True,
        )
        return JSONResponse(
            status_code=451,
            content={
                "error": "Request violates content policy",
                "code": "guard_blocked",
                "category": decision.categories[0] if decision.categories else "unknown",
                "request_id": ctx.request_id,
            },
            headers={
                f"{_HP}-Guard-Blocked": "pre",
                f"{_HP}-Guard-Category": decision.categories[0]
                if decision.categories
                else "unknown",
                f"{_HP}-Guard-Latency-Ms": str(int(decision.latency_ms)),
            },
        )

    try:
        if body.stream:
            # W2 — Stream-Setup parallel zum Pre-Guard.
            # chat_completions_stream() returnt (provider_name, resolved_model,
            # raw_stream, route_debug). raw_stream ist ein httpx-AsyncIterator —
            # der erste Chunk kommt erst beim ersten async-for. Setup-Latenz
            # (TLS-Handshake + Initial-Request) laeuft parallel zum Pre-Guard.
            stream_setup_task = asyncio.create_task(
                provider_registry.chat_completions_stream(effective_model, payload, ctx)
            )
            pre_decision = await _await_pre_decision()
            if pre_decision is not None and pre_decision.is_blocked:
                # Stream-Setup canceln, raw_stream wenn schon da explizit aclose().
                stream_setup_task.cancel()
                try:
                    _, _, raw_stream_pending, _ = await stream_setup_task
                    await _aclose_quietly(raw_stream_pending)
                except (asyncio.CancelledError, ProviderError, Exception):  # noqa: BLE001
                    pass
                return _build_pre_block_response(pre_decision)
            (
                provider_name,
                resolved_model,
                raw_stream,
                route_debug,
            ) = await stream_setup_task

            # Streaming post-guard: tail-buffer (max guard_post_stream_tail_bytes)
            # collects the last X KB of content text. After stream end the tail
            # is checked by Qwen3Guard-Gen-8B. On block: warning event sent to
            # client (output already delivered, cannot be recalled —
            # marker signals "potentially unsafe").
            stream_post_guard_enabled = (
                settings.guard_post_stream_enabled and not skip_guard and bool(user_input_text)
            )

            async def stream_iter() -> AsyncIterator[bytes]:
                sse_buffer = ""
                upstream_done_seen = False
                sample_events: list[str] = []
                tail_buffer = ""  # rolling 10KB content-tail fuer Post-Guard
                stream_was_interrupted = False
                tail_max = max(1024, int(settings.guard_post_stream_tail_bytes))
                # P3-13 (BUNDLE-A): TTFB-Messung. start_time = jetzt
                # (nach Stream-Setup), first_chunk_recorded ist Latch.
                stream_iter_start = time.perf_counter()
                first_chunk_recorded = False
                try:
                    async for chunk in raw_stream:
                        if not first_chunk_recorded:
                            ttfb_ms = (time.perf_counter() - stream_iter_start) * 1000.0
                            metrics_store.record_ttfb(provider_name, resolved_model, ttfb_ms)
                            first_chunk_recorded = True
                        text = chunk.decode("utf-8", errors="ignore")
                        if "data:" in text:
                            outgoing = chunk
                        else:
                            outgoing = f"data: {text.strip()}\n\n".encode("utf-8")
                        sse_data_lines, sse_buffer = _extract_sse_data_lines(
                            sse_buffer, outgoing.decode("utf-8", errors="ignore")
                        )
                        for data_line in sse_data_lines:
                            if data_line == "[DONE]":
                                upstream_done_seen = True
                            else:
                                if data_line and len(sample_events) < 2:
                                    sample_events.append(data_line[:200])
                                # W3: rolling Tail-Buffer fuer Post-Guard.
                                if stream_post_guard_enabled:
                                    chunk_text = _extract_streaming_chunk_text(data_line)
                                    if chunk_text:
                                        tail_buffer = (tail_buffer + chunk_text)[-tail_max:]
                        yield outgoing
                except Exception as exc:
                    stream_was_interrupted = True
                    log_event(
                        logger,
                        "stream_interrupted",
                        request_id=ctx.request_id,
                        error=str(exc)[:500],
                        error_type=type(exc).__name__,
                    )
                    error_event = {
                        "type": "error",
                        "code": "stream_interrupted",
                        "message": f"{type(exc).__name__}: {exc!s:.200}",
                    }
                    yield _sse_event(error_event)
                    yield b"data: [DONE]\n\n"
                    upstream_done_seen = True
                finally:
                    # On client disconnect, close raw_stream explicitly —
                    # otherwise the httpx AsyncClient leaks until GC.
                    await _aclose_quietly(raw_stream)

                # W3: Streaming-Post-Guard NACH Stream-Ende, aber VOR
                # finalem [DONE]. Bei Block: Warning-Event injizieren.
                # Wenn Stream interrupted war, Tail-Buffer nicht checken
                # (haette eh keinen vollstaendigen Output).
                stream_post_decision = None
                stream_post_blocked = False
                if stream_post_guard_enabled and tail_buffer and not stream_was_interrupted:
                    try:
                        stream_post_decision = await check_safety_post(
                            user_input_text,
                            tail_buffer,
                            base_url=settings.ovh_base_url,
                            api_key=settings.ovh_api_key,
                            model=settings.guard_post_model,
                            timeout_s=settings.guard_post_timeout_s,
                            fail_open=settings.guard_fail_open,
                        )
                        if stream_post_decision.is_blocked:
                            stream_post_blocked = True
                            log_event(
                                logger,
                                "guard_blocked_post_stream",
                                request_id=ctx.request_id,
                                path="/v1/chat/completions",
                                guard_safety=stream_post_decision.safety,
                                guard_categories=list(stream_post_decision.categories),
                                guard_reason=stream_post_decision.reason,
                                guard_latency_ms=int(stream_post_decision.latency_ms),
                                guard_model=stream_post_decision.model,
                            )
                            yield _sse_event(
                                {
                                    "type": "guard_warning",
                                    "code": "post_filtered",
                                    "category": (
                                        stream_post_decision.categories[0]
                                        if stream_post_decision.categories
                                        else "unknown"
                                    ),
                                    "message": "[output filtered]",
                                }
                            )
                    except Exception as exc:  # noqa: BLE001
                        log_event(
                            logger,
                            "guard_post_stream_error",
                            request_id=ctx.request_id,
                            error=str(exc)[:500],
                            error_type=type(exc).__name__,
                        )

                if not upstream_done_seen:
                    yield b"data: [DONE]\n\n"
                log_event(
                    logger,
                    "stream_termination",
                    request_id=ctx.request_id,
                    path="/v1/chat/completions",
                    provider=provider_name,
                    raw_model=body.model,
                    model=resolved_model,
                    stream=True,
                    upstream_done_seen=upstream_done_seen,
                    sampled_events=sample_events,
                    route_debug=route_debug,
                )
                # W3: dediziertes guard_audit-Event auch fuer Streaming.
                if pre_decision is not None or stream_post_decision is not None:
                    # W4: bei Streaming ist Saving die Pre-Guard-Latenz
                    # vollstaendig (Stream-Setup hat OVH-TLS-Handshake-Latenz,
                    # die war schon parallel zum Pre-Guard).
                    stream_saving_ms = (
                        int(pre_decision.latency_ms)
                        if pre_decision is not None and pre_task is not None
                        else None
                    )
                    log_event(
                        logger,
                        "guard_audit",
                        request_id=ctx.request_id,
                        path="/v1/chat/completions",
                        guard_pre=pre_decision.to_audit_dict() if pre_decision else None,
                        guard_post=(
                            stream_post_decision.to_audit_dict() if stream_post_decision else None
                        ),
                        final_allowed=not stream_post_blocked,
                        final_blocked_reason=("post_stream" if stream_post_blocked else None),
                        guard_pre_parallel=pre_task is not None,
                        guard_pre_parallel_saving_ms=stream_saving_ms,
                        stream=True,
                    )

            elapsed_ms = (time.perf_counter() - started) * 1000
            metrics_store.record(provider_name, resolved_model, 200, True, elapsed_ms)
            # Stream requests: count request, tokens not available in stream
            _track_provider_usage(provider_name, 0, 0, auth_key)
            log_event(
                logger,
                "request_completed",
                request_id=ctx.request_id,
                path="/v1/chat/completions",
                provider=provider_name,
                raw_model=body.model,
                model=resolved_model,
                latency_ms=int(elapsed_ms),
                status_code=200,
                stream=True,
                route_debug=route_debug,
            )
            return StreamingResponse(stream_iter(), media_type="text/event-stream")

        # W1 — Pre-Guard parallel zum Provider-Connect.
        # Provider-Task starten BEVOR Pre-Decision-Wait, damit beide parallel laufen.
        # Bei Pre-Block: Provider-Task canceln (CancelledError silent verschlucken).
        provider_task = asyncio.create_task(
            provider_registry.chat_completions(effective_model, payload, ctx)
        )
        pre_decision = await _await_pre_decision()
        if pre_decision is not None and pre_decision.is_blocked:
            provider_task.cancel()
            try:
                await provider_task
            except (asyncio.CancelledError, ProviderError, Exception):  # noqa: BLE001
                # Provider-Connect-Cleanup darf nicht den Pre-Block ueberlagern.
                pass
            return _build_pre_block_response(pre_decision)
        (
            provider_name,
            resolved_model,
            data,
            route_debug,
        ) = await provider_task

        # F8 — escalation cascade (non-stream only): if the first tier errored or
        # produced no usable content, walk the remaining tiers in order and stop
        # at the first usable answer. Stream requests use the first tier only
        # (a delivered stream cannot be retried).
        escalated_via: list[str] = []
        if _cascade_tiers:
            for next_tier in _cascade_tiers[1:]:
                if not _is_empty_chat_response(data):
                    break
                escalated_via.append(next_tier)
                payload["model"] = next_tier
                try:
                    provider_name, resolved_model, data, route_debug = (
                        await provider_registry.chat_completions(next_tier, payload, ctx)
                    )
                except ProviderError:
                    continue
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics_store.record(provider_name, resolved_model, 200, False, elapsed_ms)
        _record_fallback_if_needed(provider_name, route_debug)

        # Token metering + provider usage tracking
        prompt_t, completion_t = _extract_usage_from_response(data)
        _track_provider_usage(provider_name, prompt_t, completion_t, auth_key)

        usage_after = None
        if auth_key and metering_service.enabled:
            if prompt_t > 0 or completion_t > 0:
                usage_after = metering_service.record_usage(
                    auth_key,
                    prompt_tokens=prompt_t,
                    completion_tokens=completion_t,
                    model=resolved_model,
                    provider=provider_name,
                )

        # ─── Qwen3Guard post-filter ──────────────────────────────────────
        # Output check: on toxic → SAFE_FALLBACK_TEXT + header.
        # Streaming path (above) has no post-filter — tracked as future work.
        post_decision: GuardDecision | None = None
        guard_blocked_post = False
        if not skip_guard and user_input_text:
            llm_output = ""
            try:
                llm_output = data["choices"][0]["message"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                pass
            if llm_output:
                post_decision = await check_safety_post(
                    user_input_text,
                    llm_output,
                    base_url=settings.ovh_base_url,
                    api_key=settings.ovh_api_key,
                    model=settings.guard_post_model,
                    timeout_s=settings.guard_post_timeout_s,
                    fail_open=settings.guard_fail_open,
                )
                if post_decision.is_blocked:
                    guard_blocked_post = True
                    data["choices"][0]["message"]["content"] = SAFE_FALLBACK_TEXT
                    log_event(
                        logger,
                        "guard_blocked_post",
                        request_id=ctx.request_id,
                        path="/v1/chat/completions",
                        guard_safety=post_decision.safety,
                        guard_categories=list(post_decision.categories),
                        guard_reason=post_decision.reason,
                        guard_latency_ms=int(post_decision.latency_ms),
                        guard_model=post_decision.model,
                    )

        log_event(
            logger,
            "request_completed",
            request_id=ctx.request_id,
            path="/v1/chat/completions",
            provider=provider_name,
            raw_model=body.model,
            model=resolved_model,
            latency_ms=int(elapsed_ms),
            status_code=200,
            stream=False,
            route_debug=route_debug,
            guard_pre=pre_decision.to_audit_dict() if pre_decision else None,
            guard_post=post_decision.to_audit_dict() if post_decision else None,
        )
        # Dedicated guard_audit event only on active guard path, for efficient
        # audit queries ("all blocked/PII/violent" without request_completed
        # volume). request_completed remains the generic latency event.
        if (pre_decision is not None) or (post_decision is not None):
            # parallel_saving_ms is the wall-time saved vs. sequential architecture.
            # Sequential: pre_lat + provider_lat. Parallel: max(pre_lat, provider_lat).
            # Saving = min(pre_lat, provider_lat).
            saving_ms: int | None = None
            if pre_decision is not None and pre_task is not None:
                saving_ms = int(min(pre_decision.latency_ms, elapsed_ms))
            log_event(
                logger,
                "guard_audit",
                request_id=ctx.request_id,
                path="/v1/chat/completions",
                guard_pre=pre_decision.to_audit_dict() if pre_decision else None,
                guard_post=post_decision.to_audit_dict() if post_decision else None,
                final_allowed=not guard_blocked_post,
                final_blocked_reason=("post" if guard_blocked_post else None),
                guard_pre_parallel=pre_task is not None,
                guard_pre_parallel_saving_ms=saving_ms,
            )
        # ─── F6: pluggable post-hooks (AI-Act tagging, output policy …) ────
        _hook_post_tags: dict[str, str] = {}
        if guard_hooks and not body.stream:
            _hook_output = ""
            try:
                _hook_output = data["choices"][0]["message"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                pass
            if _hook_output:
                _hook_post_ctx: dict = {
                    "model": effective_model,
                    "request_id": ctx.request_id,
                }
                _hook_output_new, _hook_post_tags = run_post_hooks(
                    guard_hooks, _hook_output, _hook_post_ctx
                )
                if _hook_output_new != _hook_output:
                    try:
                        data["choices"][0]["message"]["content"] = _hook_output_new
                    except (KeyError, IndexError, TypeError):
                        pass

        resp = JSONResponse(_sanitize_tool_calls(data))
        # F6: emit all hook tags as response headers.
        for _tag_key, _tag_val in {**_hook_pre_tags, **_hook_post_tags}.items():
            resp.headers[f"{_HP}-Hook-{_tag_key}"] = _tag_val
        # F4: signal a budget-driven downgrade so clients can detect it.
        if _budget_degrade_alias:
            resp.headers[f"{_HP}-Budget-Degraded"] = _budget_degrade_alias
        # F8: report which tier the cascade settled on (if it escalated).
        if escalated_via:
            resp.headers[f"{_HP}-Escalated-To"] = resolved_model
        if guard_blocked_post and post_decision:
            resp.headers[f"{_HP}-Guard-Blocked"] = "post"
            resp.headers[f"{_HP}-Guard-Category"] = (
                post_decision.categories[0] if post_decision.categories else "unknown"
            )
        if pre_decision and not pre_decision.is_blocked:
            resp.headers[f"{_HP}-Guard-Pre-Latency-Ms"] = str(int(pre_decision.latency_ms))
        if post_decision:
            resp.headers[f"{_HP}-Guard-Post-Latency-Ms"] = str(int(post_decision.latency_ms))
        # F3: cache the (already-guarded) response for future semantically-close
        # requests. Skip if the output was guard-blocked (don't serve it again).
        if _cacheable and _cache_embedding and not guard_blocked_post:
            semantic_cache.store(ctx.request_id, _cache_embedding, data)
            resp.headers[f"{_HP}-Cache"] = "miss"
        return _add_metering_headers(resp, auth_key, usage_after)
    except ProviderError as exc:
        err = _normalize_provider_error(exc)
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics_store.record("none", body.model, err.http_status, body.stream, elapsed_ms)
        log_event(
            logger,
            "request_failed",
            request_id=ctx.request_id,
            path="/v1/chat/completions",
            provider="none",
            model=body.model,
            latency_ms=int(elapsed_ms),
            status_code=err.http_status,
            stream=body.stream,
            code=err.code,
            message=err.message,
            provider_status=_provider_status_of(exc),
            provider_response=_provider_response_excerpt(exc),
        )
        raise err


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file: UploadFile = File(...),
    model: str = Form(default="whisper-1"),
    language: str | None = Form(default=None),
    prompt: str | None = Form(default=None),
    response_format: str = Form(default="json"),
    temperature: float = Form(default=0.0),
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Proxy audio transcription via OVH Whisper (EU-DC, DSGVO).

    Pricing (OVH-Rechnung 2026-05-05): Audio wird pro Sekunde berechnet
    (audio_seconds-Achse, NICHT in /v1/models gelistet).
    - whisper-large-v3-turbo: ~0.0000216 USD/sec ≈ 0.078 USD/h
    - whisper-large-v3:       ~0.0000864 USD/sec ≈ 0.311 USD/h (Schaetzwert)

    Modell-Aliases (Konsumenten-Side):
    - "whisper-1" (OpenAI-Default) → settings.ovh_whisper_model
      = "whisper-large-v3-turbo" (schnell)
    - "whisper-large-v3-turbo" → direkter OVH-Modell-Name
    - "whisper-large-v3" → direkter OVH-Modell-Name (HQ, langsamer)
    - "voice" / "voice_hq" → Aliase fuer Konsumenten

    Datei-Limit: bis 100 MB akzeptiert. 600s Timeout (lange Aufnahmen).
    Erlaubte Mime-Types: alle audio/* + video/webm.
    """
    try:
        _enforce_internal_auth(x_api_key, authorization)
    except ProxyError as exc:
        return _error_response("audio", exc)

    audio_bytes = await file.read()
    if len(audio_bytes) == 0:
        raise HTTPException(status_code=422, detail="audio file is empty")

    api_key = _extract_api_key(x_api_key, authorization)

    if model in ("whisper-1", "voice", "whisper-low"):
        resolved_model = settings.ovh_whisper_model
    elif model in ("voice_hq", "whisper-high"):
        resolved_model = "whisper-large-v3"
    elif model in ("whisper-large-v3-turbo", "whisper-large-v3"):
        resolved_model = model
    else:
        resolved_model = model

    try:
        text, ovh_data = await _whisper_ovh(audio_bytes, file, language, resolved_model)

        if api_key and metering_service.enabled:
            try:
                # OVH berechnet Whisper per Audio-Sekunde (NICHT pro Token).
                # `data.duration` liefert OVH bei verbose_json — ohne diese
                # Info nehmen wir 0 und tracken nur den Request-Count.
                duration = float(ovh_data.get("duration") or 0)
                metering_service.record_usage(
                    api_key,
                    prompt_tokens=0,
                    completion_tokens=0,
                    model=resolved_model,
                    provider="ovh",
                    audio_seconds=duration,
                )
            except Exception:
                logger.exception("audio.transcriptions: Cost-Tracking-Update fehlgeschlagen")

        return JSONResponse(
            {
                "text": text,
                "provider": "ovh",
                "model": resolved_model,
                "language": ovh_data.get("language"),
                "duration": ovh_data.get("duration"),
                "words": ovh_data.get("words", []) if response_format == "verbose_json" else None,
                "segments": ovh_data.get("segments", [])
                if response_format == "verbose_json"
                else None,
            }
        )
    except httpx.HTTPStatusError as exc:
        # Clean ProxyError contract instead of raw text in `detail`,
        # consistent with /v1/chat/completions and /v1/embeddings.
        raise ProxyError(
            http_status=502,
            code="whisper_upstream_error",
            message=f"OVH whisper error {exc.response.status_code}",
            details={
                "provider_status": exc.response.status_code,
                "provider_response": exc.response.text[:300],
            },
        ) from exc
    except httpx.TimeoutException as exc:
        raise ProxyError(
            http_status=504,
            code="whisper_timeout",
            message=f"OVH whisper timeout: {exc}",
        ) from exc
    except httpx.RequestError as exc:
        raise ProxyError(
            http_status=503,
            code="whisper_unavailable",
            message=f"OVH whisper unavailable: {exc}",
        ) from exc


async def _whisper_ovh(
    audio_bytes: bytes,
    file: UploadFile,
    language: str | None,
    model: str,
) -> tuple[str, dict[str, Any]]:
    """Transcribe via OVH AI Endpoints Whisper (pay-per-second, EU-DC).

    OVH-Endpoint: POST /v1/audio/transcriptions (multipart/form-data).
    Returns: (text, full_response_dict) — full_dict enthaelt language,
    duration, words[], segments[], usage etc. fuer verbose_json-Format.
    """
    url = f"{settings.ovh_base_url.rstrip('/')}/audio/transcriptions"
    form_data: dict[str, str] = {"model": model}
    if language:
        form_data["language"] = language

    # Großzügig dimensioniert: 600s Timeout fuer lange Aufnahmen
    timeout = httpx.Timeout(600.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.ovh_api_key}"},
            data=form_data,
            files={
                "file": (
                    file.filename or "audio.wav",
                    audio_bytes,
                    file.content_type or "audio/wav",
                )
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return data.get("text", ""), data


@app.post("/v1/images/generations")
async def images_generations(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Generate an image via OVH Stable-Diffusion-XL (kostenlos, EU-DC).

    W3-Pre.3 (2026-05-04): Neuer OpenAI-Spec-kompatibler Image-Gen-Endpoint.
    OVH bietet SDXL aktuell kostenlos an, das Schema unterstuetzt aber
    schon `pricing.image` (Per-Image-Cost-Achse) — Cost-Tracking ist
    aktiv-ready fuer zukuenftige OVH-Pricing-Updates.

    Request (OpenAI-Spec):
    {
      "model": "stable-diffusion-xl-base-v10" oder Alias "image" (Default),
      "prompt": "..."  (Pflicht),
      "size": "512x512" oder "1024x1024" (optional, Default 1024x1024),
      "response_format": "b64_json" (Default, OVH liefert nur b64)
      // OVH-QUIRK: `n` Parameter wird abgelehnt — fuer mehrere Bilder
      // muessen Konsumenten parallele Requests senden.
    }

    Response (OpenAI-Spec):
    {
      "created": <unix-ts>,
      "data": [{"b64_json": "..."}],
      "model": "<resolved>",
      "size": "1024x1024",
      "usage": {...} // upstream reports this; all 0 for free SDXL
    }

    Headers (quota tracking, prefix configurable, default X-Orkoprox):
    - {prefix}-Images-Today: per-day image count (per-image quota)
    - {prefix}-Cost-EUR-Today / {prefix}-Cost-USD-Today: aggregate cost
    """
    try:
        _enforce_internal_auth(x_api_key, authorization)
    except ProxyError as exc:
        return _error_response("images.generations", exc)

    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="field 'prompt' required")

    # Quirk-Check: `n` wird von OVH abgelehnt, wir loggen + entfernen.
    if body.get("n") is not None and int(body.get("n", 1)) != 1:
        logger.info(
            "images.generations: `n` Parameter wird von OVH nicht unterstuetzt, "
            "ignoriert (parallel Requests senden fuer mehrere Bilder)"
        )

    model_alias = body.get("model", "image")
    if model_alias == "image" or not model_alias:
        resolved_model = settings.ovh_image_model
    else:
        # Direkter Modell-Name (z.B. "stable-diffusion-xl-base-v10")
        resolved_model = model_alias

    api_key = _extract_api_key(x_api_key, authorization)

    payload: dict[str, Any] = {
        "model": resolved_model,
        "prompt": prompt,
    }
    # Optionale Parameter (OVH-Spec)
    if body.get("size"):
        payload["size"] = body["size"]
    if body.get("quality"):
        payload["quality"] = body["quality"]
    if body.get("output_format"):
        payload["output_format"] = body["output_format"]
    if body.get("background"):
        payload["background"] = body["background"]

    ovh_url = f"{settings.ovh_base_url.rstrip('/')}/images/generations"
    timeout = httpx.Timeout(180.0, connect=10.0)  # SDXL kann 30-60s brauchen

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                ovh_url,
                headers={
                    "Authorization": f"Bearer {settings.ovh_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail=f"OVH images.generations timeout: {exc}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"OVH images.generations request error: {exc}",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502 if response.status_code >= 500 else response.status_code,
            detail=f"OVH images.generations error {response.status_code}: {response.text[:300]}",
        )

    data = response.json()
    image_count = len(data.get("data", []))

    # Cost tracking via record_usage with n_images axis.
    # record_usage is called even when image_count=0, so the call lands in
    # the quota/audit table. Without this, failed generations would consume
    # OVH quota without leaving an audit trail.
    headers_extra: dict[str, str] = {}
    if api_key and metering_service.enabled:
        try:
            usage = metering_service.record_usage(
                api_key,
                prompt_tokens=0,  # Image-Gen hat keine Tokens
                completion_tokens=0,
                model=resolved_model,
                provider="ovh",
                n_images=image_count,
            )
            config = metering_service.get_key_config(api_key)
            headers_extra = metering_service.build_usage_headers(config, usage, api_key=api_key)
        except Exception:
            logger.exception("images.generations: Cost-Tracking-Update fehlgeschlagen")

    return JSONResponse(
        {
            "created": data.get("created"),
            "data": data.get("data", []),
            "model": resolved_model,
            "size": data.get("size"),
            "usage": data.get("usage", {}),
            "model_alias": model_alias,
        },
        headers=headers_extra,
    )


@app.post("/v1/vision")
async def vision_analyze(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Analyze an image via OVH multimodal model (Mistral-Small-3.2-24B
    Default, Qwen2.5-VL-72B optional fuer komplexe Vision).

    W3-Pre.2 Hard-Cut (2026-05-04): vorher Ollama (gemma3:4b lokal),
    jetzt OVH AI Endpoints. Begruendung: Ollama-CPU-Vision >30s/Bild,
    OVH multimodal in <2s. Plus EU-DC-DSGVO statt lokaler Hardware.

    Accepts two formats:
    - multipart/form-data: fields `file` (image upload), optional `prompt`,
      optional `model` ("vision" Default oder "vision_x" fuer Premium).
    - application/json: fields `image` (base64), `mime_type` (default:
      image/jpeg), `prompt`, `model` ("vision" oder "vision_x").

    Model-Aliases:
    - "vision" oder None  → Mistral-Small-3.2-24B (Default, 131k ctx,
      $0.10/$0.31/M, multimodal)
    - "vision_x"           → Qwen2.5-VL-72B-Instruct (Premium, 32k ctx,
      $1.01/M sym., teuerstes OVH-Modell — explizit anfordern)

    Returns: OpenAI-Spec-kompatibel mit zusaetzlichem `description`-Feld
    fuer Backward-Compat:
    {
      "description": "<text>",     # backward-compat
      "model": "<resolved-model>",
      "usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N},
      "choices": [...]              # OpenAI-Spec
    }

    """
    try:
        _enforce_internal_auth(x_api_key, authorization)
    except ProxyError as exc:
        return _error_response("vision", exc)

    content_type = request.headers.get("content-type", "")
    b64_image: str = ""
    mime_type: str = "image/jpeg"
    prompt: str = "Beschreibe dieses Bild auf Deutsch. Erkenne und lies jeden sichtbaren Text, Beschriftungen und Inhalte."
    model_alias: str = "vision"

    if "multipart/form-data" in content_type:
        form = await request.form()
        file_field = form.get("file")
        if not isinstance(file_field, UploadFile):
            raise HTTPException(
                status_code=422, detail="field 'file' required for multipart upload"
            )
        image_bytes = await file_field.read()
        b64_image = __import__("base64").b64encode(image_bytes).decode()
        mime_type = file_field.content_type or "image/jpeg"
        if form.get("prompt"):
            prompt = str(form.get("prompt"))
        if form.get("model"):
            model_alias = str(form.get("model"))
    else:
        body = await request.json()
        b64_image = body.get("image", "")
        mime_type = body.get("mime_type", "image/jpeg")
        if body.get("prompt"):
            prompt = body["prompt"]
        if body.get("model"):
            model_alias = body["model"]

    if not b64_image:
        raise HTTPException(status_code=422, detail="no image data provided")

    # Modell-Resolution: vision (Default) → ovh_vision_model,
    # vision_x → ovh_vision_premium_model.
    if model_alias == "vision_x":
        resolved_model = settings.ovh_vision_premium_model
    else:
        resolved_model = settings.ovh_vision_model

    api_key = _extract_api_key(x_api_key, authorization)

    # OVH /v1/chat/completions Multimodal-Pattern. image_url-Content-Block
    # ist OpenAI-Spec-kompatibel.
    payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{b64_image}"},
                    },
                ],
            }
        ],
        "stream": False,
        "max_tokens": 1024,
    }

    ovh_url = f"{settings.ovh_base_url.rstrip('/')}/chat/completions"
    timeout = httpx.Timeout(120.0, connect=10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                ovh_url,
                headers={
                    "Authorization": f"Bearer {settings.ovh_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail=f"OVH vision timeout: {exc}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"OVH vision request error: {exc}",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502 if response.status_code >= 500 else response.status_code,
            detail=f"OVH vision error {response.status_code}: {response.text[:300]}",
        )

    data = response.json()
    description = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage", {})

    # Token metering: cost tracking via record_usage (covers OVH models).
    # Store record_usage return value in usage_after so cost headers are
    # attached to the vision response.
    usage_after = None
    if api_key and metering_service.enabled:
        try:
            usage_after = metering_service.record_usage(
                api_key,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                model=resolved_model,
                provider="ovh",
            )
        except Exception:
            logger.exception("Vision: Token-Metering-Update fehlgeschlagen")

    resp = JSONResponse(
        {
            "description": description,  # backward-compat
            "model": resolved_model,
            "usage": usage,
            "choices": data.get("choices", []),
            "model_alias": model_alias,
        }
    )
    return _add_metering_headers(resp, api_key, usage_after)


# ── Admin Endpoints for Token Metering ──────────────────────────────────────


@app.get("/v1/admin/metering/keys")
async def admin_list_metered_keys(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """List all registered metered API keys with current usage. Admin plane only."""
    enforce_admin_auth(settings, x_api_key, authorization)
    keys = metering_service.list_all_keys()
    return JSONResponse({"keys": keys, "count": len(keys)})


@app.post("/v1/admin/policy/reload")
async def admin_reload_policy(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Reload the declarative policy file if it changed. Admin plane only.

    GitOps-friendly: a deployment can push an updated orkoprox.toml and call
    this endpoint to apply alias/limit changes without a restart.
    """
    admin_key = enforce_admin_auth(settings, x_api_key, authorization)
    if not policy_loader.enabled:
        raise ProxyError(
            http_status=400,
            code="policy_disabled",
            message="no POLICY_FILE configured",
        )
    changed = policy_loader.reload_if_changed()
    if changed:
        apply_policy_to_settings(policy_loader.policy, settings)
        rate_limiter.reconfigure(
            per_minute=settings.rate_limit_per_minute,
            burst=settings.rate_limit_burst,
            concurrency=settings.rate_limit_concurrency,
        )
    audit_log.record("policy_reload", admin_key=mask_key(admin_key), changed=changed)
    return JSONResponse(
        {
            "ok": True,
            "reloaded": changed,
            "aliases": len(policy_loader.policy.aliases),
            "limits": len(policy_loader.policy.limits),
        }
    )


@app.get("/admin/stats")
async def admin_stats(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Aggregated stats for the admin dashboard. Admin plane only.

    Pulls together per-key usage, per-provider token usage, request counts,
    semantic-cache hit rate, and rate-limit config — everything the built-in
    dashboard renders, in one call. No full keys are ever returned.
    """
    enforce_admin_auth(settings, x_api_key, authorization)
    return JSONResponse(
        {
            "keys": metering_service.list_all_keys(),
            "provider_token_usage": metrics_store.get_provider_token_usage(),
            "per_key_requests": metrics_store.get_per_key_request_counts(),
            "cache": semantic_cache.stats(),
            "config": {
                "rate_limit_per_minute": settings.rate_limit_per_minute,
                "rate_limit_concurrency": settings.rate_limit_concurrency,
                "semantic_cache_enabled": settings.semantic_cache_enabled,
                "guard_enabled": settings.guard_enabled,
                "guard_hooks": settings.guard_hooks,
                "primary_provider": settings.primary_provider,
                "escalation_cascade": settings.escalation_cascade,
                "budget_degrade_alias": settings.budget_degrade_alias,
            },
        }
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard() -> HTMLResponse:
    """Serve the built-in admin dashboard (single self-contained HTML page).

    The page itself is public HTML/JS; it asks for an admin key in the browser
    and calls the admin-plane /admin/stats endpoint with it. No data is embedded
    in the page — the API enforces auth.
    """
    html_path = Path(__file__).parent / "static" / "admin.html"
    try:
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse("<h1>orkoprox</h1><p>Admin dashboard asset missing.</p>", status_code=500)


@app.post("/v1/admin/metering/keys")
async def admin_register_metered_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Register a new metered API key.

    Body: {"api_key": "...", "tenant_id": "...", "daily_token_limit": 500000, "package": "starter"}

    Admin plane only — a data-plane key cannot reach this endpoint.
    """
    admin_key = enforce_admin_auth(settings, x_api_key, authorization)
    body = await request.json()
    new_key = body.get("api_key", "")
    if not new_key or len(new_key) < 40:
        raise ProxyError(
            http_status=400, code="invalid_key", message="API key must be >= 40 characters"
        )
    config = KeyConfig(
        tenant_id=body.get("tenant_id", ""),
        daily_token_limit=int(body.get("daily_token_limit", 0)),
        package=body.get("package", ""),
        active=body.get("active", True),
    )
    metering_service.register_key(new_key, config)
    # Also add to allowed keys set so auth passes
    settings.proxy_api_keys = (
        settings.proxy_api_keys + "," + new_key if settings.proxy_api_keys else new_key
    )
    audit_log.record(
        "key_register",
        admin_key=mask_key(admin_key),
        target_key=mask_key(new_key),
        tenant_id=config.tenant_id,
        daily_token_limit=config.daily_token_limit,
    )
    return JSONResponse(
        {"ok": True, "tenant_id": config.tenant_id, "daily_token_limit": config.daily_token_limit}
    )


@app.post("/v1/admin/metering/keys/revoke")
async def admin_revoke_metered_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Revoke a metered API key. Admin plane only."""
    admin_key = enforce_admin_auth(settings, x_api_key, authorization)
    body = await request.json()
    target_key = body.get("api_key", "")
    metering_service.revoke_key(target_key)
    audit_log.record(
        "key_revoke", admin_key=mask_key(admin_key), target_key=mask_key(target_key)
    )
    return JSONResponse({"ok": True, "revoked": True})


@app.get("/v1/admin/metering/usage/{tenant_id}")
async def admin_get_tenant_usage(
    tenant_id: str,
    days: int = Query(default=7, ge=1, le=30),
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Get usage for a specific tenant over the last N days. Admin plane only."""
    enforce_admin_auth(settings, x_api_key, authorization)
    # Find key for tenant
    all_keys = metering_service.list_all_keys()
    tenant_keys = [k for k in all_keys if k["tenant_id"] == tenant_id]
    if not tenant_keys:
        return JSONResponse({"tenant_id": tenant_id, "usage": {}, "found": False})
    # Resolve the tenant's actual key via the storage backend (Redis or memory).
    result: dict[str, Any] = {"tenant_id": tenant_id, "found": True, "daily": {}}
    prefix = "metering:config:"
    for store_key in metering_service._store.scan_prefix(prefix):
        api_key = store_key[len(prefix):]
        config = metering_service.get_key_config(api_key)
        if config and config.tenant_id == tenant_id:
            usage_range = metering_service.get_usage_range(api_key, days)
            result["daily"] = {date: u.to_dict() for date, u in usage_range.items()}
            result["daily_token_limit"] = config.daily_token_limit
            result["package"] = config.package
            break
    return JSONResponse(result)


# ── F7: Drop-in Compatibility Endpoints ──────────────────────────────────────
#
# These endpoints let existing Anthropic-SDK and Ollama clients point at
# orkoprox without changing their wire shape.  Only non-streaming is supported
# in v0.1; stream=true returns HTTP 400 with a clear message.


@app.post("/v1/messages")
async def compat_anthropic_messages(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Anthropic Messages-API compatibility endpoint (F7).

    Accepts an Anthropic Messages-API request body, translates it to the
    OpenAI Chat Completions format used internally, calls the provider
    registry, and returns an Anthropic-shaped response.

    Streaming (stream=true) is not yet supported on this endpoint and returns
    HTTP 400.  Use the native /v1/chat/completions endpoint for streaming.

    Auth, rate-limiting, and budget enforcement are identical to the native
    endpoint.
    """
    auth_key = _enforce_internal_auth(x_api_key, authorization)
    _enforce_metering_budget(auth_key)

    body = await request.json()

    if body.get("stream", False):
        raise ProxyError(
            http_status=400,
            code="streaming_not_supported",
            message=(
                "streaming is not yet supported on the compatibility endpoint "
                "/v1/messages — use /v1/chat/completions with stream=true instead"
            ),
        )

    translated = anthropic_to_openai(body)
    ctx = _make_request_context(request)
    started = time.perf_counter()

    try:
        provider_name, resolved_model, data, route_debug = (
            await provider_registry.chat_completions(translated["model"], translated, ctx)
        )
    except ProviderError as exc:
        err = _normalize_provider_error(exc)
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics_store.record("none", translated.get("model", ""), err.http_status, False, elapsed_ms)
        log_event(
            logger,
            "request_failed",
            request_id=ctx.request_id,
            path="/v1/messages",
            provider="none",
            model=translated.get("model", ""),
            latency_ms=int(elapsed_ms),
            status_code=err.http_status,
        )
        raise err

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics_store.record(provider_name, resolved_model, 200, False, elapsed_ms)

    prompt_t, completion_t = _extract_usage_from_response(data)
    _track_provider_usage(provider_name, prompt_t, completion_t, auth_key)

    usage_after = None
    if auth_key and metering_service.enabled:
        if prompt_t > 0 or completion_t > 0:
            usage_after = metering_service.record_usage(
                auth_key,
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                model=resolved_model,
                provider=provider_name,
            )

    log_event(
        logger,
        "request_completed",
        request_id=ctx.request_id,
        path="/v1/messages",
        provider=provider_name,
        raw_model=body.get("model", ""),
        model=resolved_model,
        latency_ms=int(elapsed_ms),
        status_code=200,
        stream=False,
        route_debug=route_debug,
    )

    anthropic_resp = openai_to_anthropic(data, resolved_model)
    resp = JSONResponse(anthropic_resp)
    resp.headers[f"{_HP}-Compat"] = "anthropic"
    return _add_metering_headers(resp, auth_key, usage_after)


@app.post("/api/chat")
async def compat_ollama_chat(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Ollama /api/chat compatibility endpoint (F7).

    Accepts an Ollama /api/chat request body, translates it to the OpenAI
    Chat Completions format used internally, calls the provider registry, and
    returns an Ollama-shaped response.

    Streaming (stream=true) is not yet supported on this endpoint and returns
    HTTP 400.  Use the native /v1/chat/completions endpoint for streaming.

    Auth, rate-limiting, and budget enforcement are identical to the native
    endpoint.
    """
    auth_key = _enforce_internal_auth(x_api_key, authorization)
    _enforce_metering_budget(auth_key)

    body = await request.json()

    if body.get("stream", False):
        raise ProxyError(
            http_status=400,
            code="streaming_not_supported",
            message=(
                "streaming is not yet supported on the compatibility endpoint "
                "/api/chat — use /v1/chat/completions with stream=true instead"
            ),
        )

    translated = ollama_to_openai(body)
    ctx = _make_request_context(request)
    started = time.perf_counter()

    try:
        provider_name, resolved_model, data, route_debug = (
            await provider_registry.chat_completions(translated["model"], translated, ctx)
        )
    except ProviderError as exc:
        err = _normalize_provider_error(exc)
        elapsed_ms = (time.perf_counter() - started) * 1000
        metrics_store.record("none", translated.get("model", ""), err.http_status, False, elapsed_ms)
        log_event(
            logger,
            "request_failed",
            request_id=ctx.request_id,
            path="/api/chat",
            provider="none",
            model=translated.get("model", ""),
            latency_ms=int(elapsed_ms),
            status_code=err.http_status,
        )
        raise err

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics_store.record(provider_name, resolved_model, 200, False, elapsed_ms)

    prompt_t, completion_t = _extract_usage_from_response(data)
    _track_provider_usage(provider_name, prompt_t, completion_t, auth_key)

    usage_after = None
    if auth_key and metering_service.enabled:
        if prompt_t > 0 or completion_t > 0:
            usage_after = metering_service.record_usage(
                auth_key,
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                model=resolved_model,
                provider=provider_name,
            )

    log_event(
        logger,
        "request_completed",
        request_id=ctx.request_id,
        path="/api/chat",
        provider=provider_name,
        raw_model=body.get("model", ""),
        model=resolved_model,
        latency_ms=int(elapsed_ms),
        status_code=200,
        stream=False,
        route_debug=route_debug,
    )

    ollama_resp = openai_to_ollama(data, resolved_model)
    resp = JSONResponse(ollama_resp)
    resp.headers[f"{_HP}-Compat"] = "ollama"
    return _add_metering_headers(resp, auth_key, usage_after)
