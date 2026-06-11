from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from app.providers.base import (
    BaseProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderRequestContext,
)


RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatibleProvider(BaseProvider):
    # Adapter-Default: konservativ supports_vision=False. Die echte Vision-
    # Faehigkeit kommt aus der DEFAULT_PROVIDER_CAPABILITY_MATRIX (per
    # Provider-Name) und MODEL_VISION_HINTS (per Modell). Dieser Default
    # greift nur, wenn ein Provider weder in der Matrix noch in den Hints
    # erscheint — dann wird Vision konservativ verweigert statt 502 vom
    # Upstream zu provozieren.
    capabilities = ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_parallel_tool_calls=True,
        supports_response_format=True,
        supports_vision=False,
    )

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        default_model: str,
        timeout_s: int,
        requires_api_key: bool = True,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.timeout_s = timeout_s
        self.requires_api_key = requires_api_key
        self.supports_stream = True

    def _headers(self, ctx: ProviderRequestContext) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "x-request-id": ctx.request_id,
            **ctx.forward_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.requires_api_key:
            raise ProviderError(
                f"{self.name}: API key missing",
                status_code=503,
                code="provider_misconfigured",
                retryable=False,
            )
        return headers

    def _sanitize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Normalize provider quirks before request dispatch.

        Keep payload OpenAI-compatible. Strips fields some upstreams reject
        (z.B. reasoning_content) und repariert leere/kaputte tool_call args.
        Auch response_format wird auf OpenAI-Spec-2024-08 normalisiert
        (json_schema-Wrapper) — das alte {"type":"json_schema","schema":...}
        Format wird von OVH mit 422 abgelehnt.
        """
        sanitized = dict(payload)

        # OpenAI-Spec-Drift (Aug 2024) — response_format json_schema:
        # ALT (clients senden noch oft):
        #   {"type":"json_schema","schema":{...}}
        # NEU (OpenAI 2024-08, OVH erzwingt es):
        #   {"type":"json_schema","json_schema":{"name":"...","schema":{...},"strict":true}}
        # Wir normalisieren defensiv beide Varianten auf NEU.
        rf = sanitized.get("response_format")
        if isinstance(rf, dict) and rf.get("type") == "json_schema":
            if "schema" in rf and "json_schema" not in rf:
                inner_schema = rf.get("schema")
                schema_name = rf.get("name") or "response"
                strict = rf.get("strict", True)
                sanitized["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": inner_schema,
                        "strict": bool(strict),
                    },
                }
            elif "json_schema" in rf and isinstance(rf["json_schema"], dict):
                # Schon korrekt verschachtelt — ggf. fehlende 'name' ergänzen
                inner = dict(rf["json_schema"])
                if "name" not in inner:
                    inner["name"] = "response"
                if "strict" not in inner:
                    inner["strict"] = True
                sanitized["response_format"] = {
                    "type": "json_schema",
                    "json_schema": inner,
                }

        # Strip reasoning_content from messages — some LLMs (gpt-oss-120b) return
        # this field, and clients may echo it back in conversation history.
        # Upstream providers reject unknown fields.
        # Also fix empty/invalid tool_call arguments in conversation history
        # to prevent "invalid tool call arguments" 400 from upstream providers.
        messages = sanitized.get("messages")
        if isinstance(messages, list):
            cleaned: list[Any] = []
            for msg in messages:
                if not isinstance(msg, dict):
                    cleaned.append(msg)
                    continue
                msg = {k: v for k, v in msg.items() if k != "reasoning_content"}
                tc_list = msg.get("tool_calls")
                if isinstance(tc_list, list):
                    for tc in tc_list:
                        fn = tc.get("function") if isinstance(tc, dict) else None
                        if isinstance(fn, dict):
                            args = fn.get("arguments", "")
                            # Some upstreams send tool-call arguments as a dict,
                            # but the OpenAI spec requires a JSON string. Normalise
                            # defensively, else multi-turn tool history is rejected.
                            if isinstance(args, dict):
                                try:
                                    args = json.dumps(args, ensure_ascii=False)
                                    fn["arguments"] = args
                                except (TypeError, ValueError):
                                    fn["arguments"] = "{}"
                                    args = "{}"
                            if not args or not isinstance(args, str) or not args.strip():
                                fn["arguments"] = "{}"
                            else:
                                try:
                                    json.loads(args)
                                except (json.JSONDecodeError, TypeError):
                                    fn["arguments"] = "{}"
                cleaned.append(msg)
            sanitized["messages"] = cleaned

        tools = sanitized.get("tools")
        if isinstance(tools, list):
            normalized_tools: list[dict[str, Any]] = []
            for tool in tools:
                if not isinstance(tool, dict):
                    normalized_tools.append(tool)
                    continue
                if tool.get("type") != "function":
                    normalized_tools.append(tool)
                    continue
                function = tool.get("function")
                if isinstance(function, dict) and function.get("name"):
                    normalized_tools.append(tool)
                    continue
                normalized_function = {
                    key: tool[key]
                    for key in ("name", "description", "parameters", "strict")
                    if key in tool
                }
                normalized_tools.append({"type": "function", "function": normalized_function})
            sanitized["tools"] = normalized_tools

        tool_choice = sanitized.get("tool_choice")
        if tool_choice and not sanitized.get("tools"):
            # tool_choice without tools causes 400 on all providers — strip it
            sanitized.pop("tool_choice", None)
        elif (
            isinstance(tool_choice, dict)
            and tool_choice.get("type") == "function"
            and not isinstance(tool_choice.get("function"), dict)
        ):
            function_choice = {
                key: tool_choice[key]
                for key in ("name", "description", "parameters", "strict")
                if key in tool_choice
            }
            if function_choice:
                sanitized["tool_choice"] = {"type": "function", "function": function_choice}

        return sanitized

    @staticmethod
    def _parse_error_payload(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            decoded = json.loads(body.decode("utf-8", errors="ignore"))
            if isinstance(decoded, dict):
                return decoded
        except Exception:
            pass
        return {"body": body[:500].decode("utf-8", errors="ignore")}

    def _extract_error_message(self, details: dict[str, Any], status: int) -> str:
        """Extrahiere eine menschenlesbare Fehlermeldung aus dem Upstream-Body.

        Drei Formate werden verstanden (DRY — eine Stelle fuer alle Aufrufer):

        1. OpenAI-Standard:     ``{"error": {"message": "..."}}``
        2. OVH-Verschachtelt:   ``{"message": "<json-string mit error-objekt>"}``
           Der Top-Level-Key ``message`` traegt einen JSON-STRING, dessen
           geparstes Objekt wiederum ``error.message`` enthaelt. Beispiel:
           ``{"message": "{\\"error\\": {\\"message\\": \\"max_tokens must be ...\\"}}"}``
        3. Roher String:        ``{"message": "..."}`` (kein nested JSON)

        Fallback-Kette: error.message -> nested error.message -> roher
        message-String (truncated) -> ``f"{self.name} error {status}"``.
        """
        # Format 1: OpenAI-Standard {"error": {"message": ...}}
        error_obj = details.get("error")
        if isinstance(error_obj, dict):
            msg = error_obj.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg

        # Format 2/3: Top-Level "message" — bei OVH ein JSON-String mit
        # verschachteltem error-Objekt, sonst ein roher Fehlertext.
        raw_message = details.get("message")
        if isinstance(raw_message, str) and raw_message.strip():
            try:
                nested = json.loads(raw_message)
                if isinstance(nested, dict):
                    nested_error = nested.get("error")
                    if isinstance(nested_error, dict):
                        nested_msg = nested_error.get("message")
                        if isinstance(nested_msg, str) and nested_msg.strip():
                            return nested_msg
            except Exception:
                pass
            # Format 3: roher message-String (truncated, kein Prompt-Inhalt).
            return raw_message[:500]

        return f"{self.name} error {status}"

    async def chat_completions(
        self, payload: dict[str, Any], ctx: ProviderRequestContext
    ) -> dict[str, Any]:
        payload = self._sanitize_payload(payload)
        if not payload.get("model"):
            payload["model"] = self.default_model

        url = f"{self.base_url}/chat/completions"
        timeout = httpx.Timeout(self.timeout_s)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=self._headers(ctx), json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.name} timeout",
                status_code=504,
                code="upstream_timeout",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.name} network error",
                status_code=502,
                code="upstream_network_error",
                retryable=True,
            ) from exc

        if resp.status_code >= 400:
            details = self._parse_error_payload(resp.content)
            message = self._extract_error_message(details, resp.status_code)
            raise ProviderError(
                message,
                status_code=502,
                code="upstream_error",
                retryable=resp.status_code in RETRYABLE_STATUS,
                details={"provider_status": resp.status_code, "provider_response": details},
            )

        return resp.json()

    async def chat_completions_stream(
        self,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> AsyncIterator[bytes]:
        payload = self._sanitize_payload(payload)
        if not payload.get("model"):
            payload["model"] = self.default_model
        payload["stream"] = True

        url = f"{self.base_url}/chat/completions"
        timeout = httpx.Timeout(self.timeout_s)
        client = httpx.AsyncClient(timeout=timeout)

        try:
            async with client.stream("POST", url, headers=self._headers(ctx), json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    details = self._parse_error_payload(body)
                    message = self._extract_error_message(details, resp.status_code)
                    raise ProviderError(
                        message,
                        status_code=502,
                        code="upstream_error",
                        retryable=resp.status_code in RETRYABLE_STATUS,
                        details={"provider_status": resp.status_code, "provider_response": details},
                    )
                async for chunk in resp.aiter_raw():
                    if chunk:
                        yield chunk
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.name} timeout",
                status_code=504,
                code="upstream_timeout",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.name} network error",
                status_code=502,
                code="upstream_network_error",
                retryable=True,
            ) from exc
        finally:
            await client.aclose()

    async def embeddings(
        self,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> dict[str, Any]:
        payload = self._sanitize_payload(payload)
        if not payload.get("model"):
            payload["model"] = self.default_model

        url = f"{self.base_url}/embeddings"
        timeout = httpx.Timeout(self.timeout_s)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=self._headers(ctx), json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.name} timeout",
                status_code=504,
                code="upstream_timeout",
                retryable=True,
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.name} network error",
                status_code=502,
                code="upstream_network_error",
                retryable=True,
            ) from exc

        if resp.status_code >= 400:
            details = self._parse_error_payload(resp.content)
            message = self._extract_error_message(details, resp.status_code)
            raise ProviderError(
                message,
                status_code=502,
                code="upstream_error",
                retryable=resp.status_code in RETRYABLE_STATUS,
                details={"provider_status": resp.status_code, "provider_response": details},
            )

        return resp.json()
