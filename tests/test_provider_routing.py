from __future__ import annotations


from app.config import Settings
from app.providers.base import BaseProvider, ProviderCapabilities, ProviderRequestContext
from app.providers.router import ProviderRegistry


def test_model_policy_passes_payload_through_unchanged() -> None:
    """No max_tokens override — OVH respects client-provided max_tokens natively.

    Policy is effectively a pass-through except for the health-probe bypass marker.
    """
    settings = Settings(proxy_api_keys="")
    registry = ProviderRegistry(settings)
    payload = {
        "model": "Mistral-Small-3.2-24B-Instruct-2506",
        "temperature": 0.1,
        "max_tokens": 256,
        "response_format": {"type": "json_schema", "schema": {"type": "object"}},
        "messages": [{"role": "user", "content": "Hello"}],
    }

    adjusted, policy = registry._apply_model_policy(
        "Mistral-Small-3.2-24B-Instruct-2506", payload
    )

    # Alle Werte unverändert durchgereicht (kein Apriel-Forcing,
    # kein max_tokens-Floor, kein response_format-Override).
    assert adjusted["temperature"] == 0.1
    assert adjusted["max_tokens"] == 256
    assert adjusted["response_format"] == {"type": "json_schema", "schema": {"type": "object"}}
    assert adjusted["messages"][0] == {"role": "user", "content": "Hello"}
    assert policy == {}


def test_model_policy_emits_health_probe_marker() -> None:
    """Health-Probes werden noch markiert (für Telemetrie), aber ohne Verhaltens-Override."""

    settings = Settings(proxy_api_keys="")
    registry = ProviderRegistry(settings)
    payload = {
        "model": "Mistral-Small-3.2-24B-Instruct-2506",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "ping"}],
    }
    ctx = ProviderRequestContext(
        request_id="probe", forward_headers={}, is_health_probe=True
    )

    adjusted, policy = registry._apply_model_policy(
        "Mistral-Small-3.2-24B-Instruct-2506", payload, ctx=ctx
    )

    assert adjusted["max_tokens"] == 5
    assert policy == {"health_probe_bypass": True}


class _FakeProvider(BaseProvider):
    def __init__(self, name: str, capabilities: ProviderCapabilities):
        self.name = name
        self.capabilities = capabilities

    async def chat_completions(self, payload, ctx):
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": payload["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }

    async def chat_completions_stream(self, payload, ctx):
        yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def embeddings(self, payload, ctx):
        return {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
            "model": payload["model"],
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }


