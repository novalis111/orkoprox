"""TEI-Reranker-Provider — self-hosted Cross-Encoder-Reranking.

Spricht den `reranker`-Docker-Sidecar an (HuggingFace Text-Embeddings-
Inference, Modell BAAI/bge-reranker-v2-m3). TEI bietet einen dedizierten
`POST /rerank`-Endpoint, der KEIN OpenAI-kompatibler Endpoint ist —
deshalb ein eigener, schlanker Provider statt OpenAICompatibleProvider.

Analogie: das ist fuer Reranking, was der Whisper-Sidecar fuer ASR ist.
OVH + Mistral haben KEINEN Rerank-Endpoint (verifiziert: 404), darum
self-hosted.

# VERIFY: TEI /rerank-Vertrag (cpu-1.5).
#   Request : {"query": str, "texts": [str], "raw_scores": bool,
#              "return_text": bool, "truncate": bool}
#   Response: [{"index": int, "score": float, "text": str?}, ...]
#   Quelle: HuggingFace TEI OpenAPI-Doku. Konnte zur Implementierungszeit
#   nicht 1:1 aus der gerenderten Doku verifiziert werden — bei einem
#   Sidecar-Upgrade gegen die TEI-OpenAPI-Spec gegenpruefen.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderRequestContext,
)


class RerankerProvider(BaseProvider):
    """Dedizierter Reranking-Provider fuer den TEI-Sidecar.

    Implementiert NUR ``rerank()``. Chat/Embeddings/Streaming sind bewusst
    nicht unterstuetzt — dieser Provider taucht ausschliesslich im
    Rerank-Routing auf, nie in der Chat-Fallback-Kette.
    """

    capabilities = ProviderCapabilities(
        supports_stream=False,
        supports_tools=False,
        supports_parallel_tool_calls=False,
        supports_response_format=False,
        supports_vision=False,
    )

    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        timeout_s: float,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.supports_stream = False

    async def chat_completions(
        self, payload: dict[str, Any], ctx: ProviderRequestContext
    ) -> dict[str, Any]:
        raise ProviderError(
            f"{self.name} is a rerank-only provider",
            status_code=501,
            code="chat_unsupported",
        )

    async def chat_completions_stream(self, payload: dict[str, Any], ctx: ProviderRequestContext):  # type: ignore[override]
        raise ProviderError(
            f"{self.name} is a rerank-only provider",
            status_code=501,
            code="chat_unsupported",
        )
        yield b""  # unreachable — keeps this an async generator

    async def embeddings(
        self, payload: dict[str, Any], ctx: ProviderRequestContext
    ) -> dict[str, Any]:
        raise ProviderError(
            f"{self.name} is a rerank-only provider",
            status_code=501,
            code="embeddings_unsupported",
        )

    async def rerank(
        self,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> dict[str, Any]:
        """Reranke ``documents`` gegen ``query`` via TEI-Sidecar.

        ``payload`` ist Cohere-/Jina-Form (``query``, ``documents``,
        optional ``top_n``, ``return_documents``). Rueckgabe ist die
        Cohere-/Jina-kompatible Response (``results``, ``model``, ``usage``).
        """
        query = payload.get("query") or ""
        documents = payload.get("documents") or []
        top_n = payload.get("top_n")
        return_documents = bool(payload.get("return_documents", False))

        # TEI-Rerank-Request — siehe # VERIFY oben.
        tei_body: dict[str, Any] = {
            "query": query,
            "texts": list(documents),
            "return_text": False,
            "raw_scores": False,
            "truncate": True,
        }

        url = f"{self.base_url}/rerank"
        timeout = httpx.Timeout(self.timeout_s, connect=10.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "x-request-id": ctx.request_id,
                    },
                    json=tei_body,
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.name} timeout",
                status_code=504,
                code="reranker_timeout",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            # Sidecar down / Netzwerk weg — klare, eindeutige Fehlersignatur.
            raise ProviderError(
                f"{self.name} unavailable: {exc}",
                status_code=503,
                code="reranker_unavailable",
                retryable=False,
            ) from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.name} error {resp.status_code}",
                status_code=502,
                code="reranker_upstream_error",
                retryable=resp.status_code in {500, 502, 503, 504},
                details={"provider_status": resp.status_code},
            )

        try:
            tei_results = resp.json()
        except ValueError as exc:
            raise ProviderError(
                f"{self.name}: invalid JSON response",
                status_code=502,
                code="reranker_upstream_error",
            ) from exc

        if not isinstance(tei_results, list):
            raise ProviderError(
                f"{self.name}: unexpected response shape",
                status_code=502,
                code="reranker_upstream_error",
            )

        # TEI liefert bereits nach score absteigend sortiert; defensiv
        # nochmal sortieren, falls ein Sidecar-Update das aendert.
        ranked = sorted(
            tei_results,
            key=lambda item: float(item.get("score", 0.0)),
            reverse=True,
        )
        if isinstance(top_n, int) and top_n > 0:
            ranked = ranked[:top_n]

        results: list[dict[str, Any]] = []
        for item in ranked:
            idx = int(item.get("index", 0))
            result: dict[str, Any] = {
                "index": idx,
                "relevance_score": float(item.get("score", 0.0)),
            }
            if return_documents and 0 <= idx < len(documents):
                result["document"] = {"text": documents[idx]}
            results.append(result)

        # Token-Metering grob: Cross-Encoder verarbeitet query + jedes Dokument.
        # TEI liefert kein usage-Feld → Zeichen/4 als Heuristik (siehe
        # offene Punkte im Bericht).
        approx_tokens = (len(query) + sum(len(d) for d in documents)) // 4

        return {
            "model": payload.get("model") or self.model,
            "results": results,
            "usage": {
                "prompt_tokens": approx_tokens,
                "total_tokens": approx_tokens,
            },
        }
