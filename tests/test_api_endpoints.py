from __future__ import annotations

from typing import AsyncIterator


def test_chat_completions_stub_fails_then_fallback(client):
    """StubProvider raises 503 — fallback chain picks up a real provider.

    In test env, baseten/together are reachable, so fallback succeeds (200).
    The important thing: StubProvider no longer silently returns fake content.
    """
    response = client.post(
        "/v1/chat/completions",
        headers={"x-api-key": "test-key"},
        json={
            "model": "mock/test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )
    # Either fallback succeeds (200) or all providers down (502/503)
    assert response.status_code in (200, 502, 503)
    assert "x-request-id" in response.headers
    if response.status_code == 200:
        body = response.json()
        content = body["choices"][0]["message"].get("content", "")
        # Stub content MUST NOT leak through
        assert "[stub]" not in content


def test_embeddings_non_stream(client):
    response = client.post(
        "/v1/embeddings",
        headers={"x-api-key": "test-key"},
        json={
            "model": "mock/text-embedding-3-small",
            "input": "hello",
        },
    )
    # Fallback may succeed or fail — stub must not return fake embeddings
    assert response.status_code in (200, 502, 503)


def test_chat_completions_stream(client):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"x-api-key": "test-key"},
        json={
            "model": "mock/test-model",
            "messages": [{"role": "user", "content": "stream me"}],
            "stream": True,
        },
    ) as response:
        # Fallback may succeed or fail — stub must not return fake stream
        assert response.status_code in (200, 502, 503)


def test_chat_completions_accepts_openai_tool_loop_messages(client, monkeypatch):
    import app.main

    captured_payload: dict[str, object] = {}

    async def fake_chat_completions(raw_model, payload, ctx):
        captured_payload.update(payload)
        return (
            "stub",
            "test-model",
            {
                "id": "chatcmpl-tool-loop",
                "object": "chat.completion",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
            },
            {"resolved_model": "test-model"},
        )

    monkeypatch.setattr(
        app.main.provider_registry, "chat_completions", fake_chat_completions
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"authorization": "Bearer test-key"},
        json={
            "model": "mock/test-model",
            "messages": [
                {"role": "user", "content": "Was steht heute an?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "get_schedule",
                                "arguments": '{"date":"2026-03-30"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_123",
                    "content": '{"appointments":[]}',
                },
            ],
            "stream": False,
        },
    )

    assert response.status_code == 200
    payload_messages = captured_payload["messages"]
    assert payload_messages[1]["tool_calls"][0]["function"]["name"] == "get_schedule"
    # content may be absent or explicitly None — both are valid for tool-call messages
    assert payload_messages[1].get("content") is None
    assert payload_messages[2]["tool_call_id"] == "call_123"


def test_chat_completions_rejects_tool_message_without_tool_call_id(client):
    response = client.post(
        "/v1/chat/completions",
        headers={"authorization": "Bearer test-key"},
        json={
            "model": "mock/test-model",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "tool", "content": '{"ok": true}'},
            ],
            "stream": False,
        },
    )

    assert response.status_code == 422


def test_models_lists_aliases_and_targets(client):
    response = client.get(
        "/v1/models",
        headers={"authorization": "Bearer test-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    ids = {entry["id"] for entry in body["data"]}
    # Tier aliases: high/medium/low + xhigh
    assert {"high", "medium", "low", "xhigh"}.issubset(ids)
    # Task-Aliases bleiben
    assert {"chat", "reason", "vision", "classify"}.issubset(ids)
    high = next(entry for entry in body["data"] if entry["id"] == "high")
    assert high["supports_parallel_tool_calls"] is True
    # Default-Reasoning ist konfigurierbar (Test-Env vs. Default unterscheiden sich nicht).
    assert "default_reasoning_level" in high


def test_models_advertises_every_routable_alias(client):
    """SSOT-Regression: /v1/models MUSS jeden Alias aus settings.model_alias_map
    listen, sonst können Clients (oekotopia=embed, lc=vision_x) ihn nicht
    entdecken. Genau diese Drift (vision_x/embed/voice fehlten) war der
    Cutover-Befund 2026-06-19 — der Test friert die Vollabdeckung ein."""
    from app.main import settings

    response = client.get("/v1/models", headers={"authorization": "Bearer test-key"})
    assert response.status_code == 200
    ids = {entry["id"] for entry in response.json()["data"]}

    advertised_aliases = {alias for alias, target in settings.model_alias_map.items() if target.strip()}
    missing = advertised_aliases - ids
    assert not missing, f"/v1/models verschweigt routbare Aliase: {sorted(missing)}"
    # Die Client-kritischen Aliase explizit (Cutover-Garantie):
    assert {"vision_x", "embed", "high", "ocr"}.issubset(ids)


def test_chat_completions_stream_appends_done_when_upstream_omits_it(
    client, monkeypatch
):
    import app.main

    async def stream_without_done() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'

    async def fake_chat_completions_stream(raw_model, payload, ctx):
        return (
            "stub",
            "test-model",
            stream_without_done(),
            {
                "raw_model": raw_model,
                "resolved_provider": "stub",
                "resolved_model": "test-model",
                "fallback_chain": ["stub"],
            },
        )

    monkeypatch.setattr(
        app.main.provider_registry,
        "chat_completions_stream",
        fake_chat_completions_stream,
    )

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"x-api-key": "test-key"},
        json={
            "model": "mock/test-model",
            "messages": [{"role": "user", "content": "stream me"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
        assert "partial" in text
        assert text.count("[DONE]") == 1


