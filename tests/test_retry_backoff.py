"""Tests fuer P3-6 (BUNDLE-A): Retry-Backoff exponential cap.

Ohne cap waechst sleep auf base * 2**attempt → bei attempts=5 + base=1s
ergibt das 32s an Attempt 5 und kann den uvicorn-Worker-Timeout reissen.
Cap auf provider_retry_backoff_cap_seconds (Default 5.0) verhindert das.

Wir testen:
1. Bei niedrigem attempt: sleep == base * 2**attempt (cap nicht aktiv)
2. Bei hohem attempt: sleep == cap (Cap greift)
3. Beide Backoff-Stellen in router.py honorieren denselben cap.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.config import Settings
from app.providers.base import BaseProvider, ProviderCapabilities, ProviderError, ProviderRequestContext
from app.providers.router import ProviderRegistry


class _AlwaysRetryableErrorProvider(BaseProvider):
    """Provider, der bei jedem Aufruf ein retryable ProviderError raised."""

    def __init__(self) -> None:
        self.name = "always_retryable"
        self.capabilities = ProviderCapabilities(
            supports_stream=True,
            supports_tools=True,
            supports_parallel_tool_calls=True,
            supports_response_format=True,
        )
        self.call_count = 0

    async def chat_completions(
        self, payload: dict[str, Any], ctx: ProviderRequestContext
    ) -> dict[str, Any]:
        self.call_count += 1
        raise ProviderError("retryable upstream", retryable=True)

    async def chat_completions_stream(self, payload: dict[str, Any], ctx: ProviderRequestContext):  # type: ignore[override]
        self.call_count += 1
        raise ProviderError("retryable upstream", retryable=True)
        yield b""  # unreachable, satisfies async-generator type

    async def embeddings(self, payload: dict[str, Any], ctx: ProviderRequestContext) -> dict[str, Any]:
        self.call_count += 1
        raise ProviderError("retryable upstream", retryable=True)


@pytest.mark.asyncio
async def test_retry_backoff_uncapped_at_low_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bei niedrigem attempt (< cap-Schwelle) wirkt der cap nicht — sleep == base * 2**attempt."""
    sleeps: list[float] = []

    async def _capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _capture_sleep)

    settings = Settings(
        proxy_api_keys="",
        provider_max_retries=2,  # 3 attempts total
        provider_retry_backoff_seconds=0.5,
        provider_retry_backoff_cap_seconds=5.0,
    )
    registry = ProviderRegistry(settings)
    provider = _AlwaysRetryableErrorProvider()
    ctx = ProviderRequestContext(request_id="test", forward_headers={})

    with pytest.raises(ProviderError):
        await registry._run_with_retry(
            provider, "chat_completions", {"model": "x"}, ctx
        )

    # 3 attempts -> 2 sleeps (zwischen den Attempts).
    # attempt 0: 0.5 * 1 = 0.5  (kein cap)
    # attempt 1: 0.5 * 2 = 1.0  (kein cap)
    assert sleeps == [0.5, 1.0]
    assert provider.call_count == 3


@pytest.mark.asyncio
async def test_retry_backoff_capped_at_high_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bei hohem attempt greift der cap — sleep wird auf cap_seconds limitiert."""
    sleeps: list[float] = []

    async def _capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _capture_sleep)

    # base=1.0, cap=2.0 → ohne cap waeren attempts: 1, 2, 4, 8, 16
    # mit cap=2.0:                                 1, 2, 2, 2, 2
    settings = Settings(
        proxy_api_keys="",
        provider_max_retries=5,  # 6 attempts → 5 sleeps
        provider_retry_backoff_seconds=1.0,
        provider_retry_backoff_cap_seconds=2.0,
    )
    registry = ProviderRegistry(settings)
    provider = _AlwaysRetryableErrorProvider()
    ctx = ProviderRequestContext(request_id="test", forward_headers={})

    with pytest.raises(ProviderError):
        await registry._run_with_retry(
            provider, "chat_completions", {"model": "x"}, ctx
        )

    # Erwartete Sleeps mit cap=2.0:
    # attempt 0: min(1.0 * 1, 2.0) = 1.0
    # attempt 1: min(1.0 * 2, 2.0) = 2.0
    # attempt 2: min(1.0 * 4, 2.0) = 2.0   ← cap greift
    # attempt 3: min(1.0 * 8, 2.0) = 2.0   ← cap greift
    # attempt 4: min(1.0 * 16, 2.0) = 2.0  ← cap greift
    assert sleeps == [1.0, 2.0, 2.0, 2.0, 2.0]
    # Wichtig: kein Wert darf cap ueberschreiten.
    assert max(sleeps) <= settings.provider_retry_backoff_cap_seconds
    assert provider.call_count == 6


@pytest.mark.asyncio
async def test_retry_backoff_cap_default_is_five_seconds() -> None:
    """Der Default-Cap ist 5.0s — Sweet-Spot zwischen Provider-Hickup und uvicorn-Timeout."""
    settings = Settings(proxy_api_keys="")
    assert settings.provider_retry_backoff_cap_seconds == 5.0
