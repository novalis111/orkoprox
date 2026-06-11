from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.providers.base import BaseProvider, ProviderCapabilities, ProviderError, ProviderRequestContext


class StubProvider(BaseProvider):
    """Placeholder provider that always fails with 503.

    If a request reaches the StubProvider, ALL real providers in the
    fallback chain have failed. This MUST be a hard error, not a fake
    200 with stub content — fake 200s silently break downstream clients.
    """

    capabilities = ProviderCapabilities(
        supports_stream=False,
        supports_tools=False,
        supports_parallel_tool_calls=False,
        supports_response_format=False,
    )

    def __init__(self, name: str = "stub", message: str = "provider not implemented yet"):
        self.name = name
        self._message = message
        self.supports_stream = False

    def _fail(self) -> ProviderError:
        return ProviderError(
            f"[{self.name}] {self._message}",
            status_code=503,
            code="stub_provider",
            retryable=False,
        )

    async def chat_completions(self, payload: dict[str, Any], ctx: ProviderRequestContext) -> dict[str, Any]:
        raise self._fail()

    async def chat_completions_stream(
        self,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> AsyncIterator[bytes]:
        raise self._fail()
        yield b""  # unreachable — keeps the function as an async generator

    async def embeddings(
        self,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> dict[str, Any]:
        raise self._fail()
