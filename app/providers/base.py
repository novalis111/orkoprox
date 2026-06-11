from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_stream: bool = True
    supports_tools: bool = True
    supports_parallel_tool_calls: bool = True
    supports_response_format: bool = True
    # Provider-level flag: can ANY model of this provider handle vision?
    #   ovh=True (Mistral-Small-3.2 + Qwen2.5-VL),
    #   ollama=True (gemma3:4b, but too slow for live load → excluded),
    #   stub=False (test provider).
    # Default False, damit unbekannte Provider nicht versehentlich Vision-Routes
    # bekommen. Modell-Level-Filter trifft die exakte Auswahl pro Modell.
    supports_vision: bool = False


@dataclass
class ProviderRequestContext:
    request_id: str
    forward_headers: dict[str, str]
    # Health-Probe-Flag: wenn gesetzt, umgeht die Policy den
    # min_completion_tokens-Floor. Wird via X-Health-Probe: 1 Header aktiviert.
    # Liveness-Checks sollen mit max_tokens=5 billig bleiben.
    is_health_probe: bool = False


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        code: str = "upstream_error",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class BaseProvider(ABC):
    name: str
    capabilities = ProviderCapabilities()

    @abstractmethod
    async def chat_completions(self, payload: dict[str, Any], ctx: ProviderRequestContext) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def chat_completions_stream(
        self,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> AsyncIterator[bytes]:
        raise NotImplementedError
        yield  # pragma: no cover — makes this an async generator for type checkers

    @abstractmethod
    async def embeddings(
        self,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def rerank(
        self,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> dict[str, Any]:
        """Cross-Encoder-Reranking.

        Default: nicht unterstuetzt. Nur dedizierte Reranker-Provider
        (TEI-Sidecar) ueberschreiben das. Bewusst KEIN @abstractmethod —
        bestehende Chat-/Embedding-Provider muessen es nicht implementieren.
        """
        raise ProviderError(
            f"{getattr(self, 'name', 'provider')} does not support rerank",
            status_code=501,
            code="rerank_unsupported",
        )
