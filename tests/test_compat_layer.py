# tests/test_compat_layer.py
"""Tests for the F7 drop-in compatibility layer (app/compat.py + endpoints).

Structure:
  1. Pure unit tests for each translation function (no I/O, no FastAPI).
  2. Endpoint integration tests via TestClient with a monkeypatched
     provider_registry.chat_completions that returns a deterministic fake
     OpenAI response — so the full translation pipeline is exercised end-to-end
     without needing a real backend.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.compat import (
    anthropic_to_openai,
    ollama_to_openai,
    openai_to_anthropic,
    openai_to_ollama,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

_FAKE_OPENAI_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-test123",
    "object": "chat.completion",
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from the proxy!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 7,
        "total_tokens": 17,
    },
}


async def _fake_chat_completions(raw_model: str, payload: dict[str, Any], ctx: Any):
    """Deterministic fake that mimics provider_registry.chat_completions."""
    return ("mock", "test-model", dict(_FAKE_OPENAI_RESPONSE), {"resolved_model": "test-model"})


# ─── 1. anthropic_to_openai ───────────────────────────────────────────────────


class TestAnthropicToOpenAI:
    def test_system_string_becomes_system_message(self):
        body = {
            "model": "claude-opus",
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 100,
        }
        result = anthropic_to_openai(body)
        assert result["messages"][0] == {"role": "system", "content": "You are helpful."}
        assert result["messages"][1] == {"role": "user", "content": "Hi"}

    def test_system_absent_no_system_message(self):
        body = {
            "model": "claude-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 50,
        }
        result = anthropic_to_openai(body)
        assert result["messages"][0]["role"] == "user"
        assert all(m["role"] != "system" for m in result["messages"])

    def test_list_content_joined_to_string(self):
        body = {
            "model": "claude-sonnet",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Part one."},
                        {"type": "text", "text": "Part two."},
                    ],
                }
            ],
            "max_tokens": 50,
        }
        result = anthropic_to_openai(body)
        assert result["messages"][0]["content"] == "Part one.\nPart two."

    def test_non_text_blocks_stripped(self):
        body = {
            "model": "claude-sonnet",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "data": "..."}},
                        {"type": "text", "text": "Describe this."},
                    ],
                }
            ],
            "max_tokens": 50,
        }
        result = anthropic_to_openai(body)
        assert result["messages"][0]["content"] == "Describe this."

    def test_max_tokens_passed_through(self):
        body = {"model": "m", "messages": [], "max_tokens": 512}
        result = anthropic_to_openai(body)
        assert result["max_tokens"] == 512

    def test_temperature_passed_through(self):
        body = {"model": "m", "messages": [], "temperature": 0.7}
        result = anthropic_to_openai(body)
        assert result["temperature"] == 0.7

    def test_model_passed_through(self):
        body = {"model": "claude-3-7-sonnet-latest", "messages": []}
        result = anthropic_to_openai(body)
        assert result["model"] == "claude-3-7-sonnet-latest"

    def test_stream_false_by_default(self):
        body = {"model": "m", "messages": []}
        result = anthropic_to_openai(body)
        assert result["stream"] is False

    def test_stream_true_passed_through(self):
        body = {"model": "m", "messages": [], "stream": True}
        result = anthropic_to_openai(body)
        assert result["stream"] is True

    def test_empty_messages_safe(self):
        body = {"model": "m", "messages": []}
        result = anthropic_to_openai(body)
        assert result["messages"] == []

    def test_anthropic_tool_remapped_to_openai_shape(self):
        body = {
            "model": "m",
            "messages": [],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Returns weather",
                    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                }
            ],
        }
        result = anthropic_to_openai(body)
        assert result["tools"][0]["type"] == "function"
        assert result["tools"][0]["function"]["name"] == "get_weather"
        assert result["tools"][0]["function"]["parameters"]["type"] == "object"

    def test_already_openai_tool_passed_through(self):
        openai_tool = {
            "type": "function",
            "function": {"name": "foo", "description": "bar", "parameters": {}},
        }
        body = {"model": "m", "messages": [], "tools": [openai_tool]}
        result = anthropic_to_openai(body)
        assert result["tools"][0] == openai_tool


# ─── 2. openai_to_anthropic ───────────────────────────────────────────────────


class TestOpenAIToAnthropic:
    def test_content_becomes_text_block(self):
        result = openai_to_anthropic(_FAKE_OPENAI_RESPONSE, "test-model")
        assert result["content"] == [{"type": "text", "text": "Hello from the proxy!"}]

    def test_role_is_assistant(self):
        result = openai_to_anthropic(_FAKE_OPENAI_RESPONSE, "test-model")
        assert result["role"] == "assistant"

    def test_type_is_message(self):
        result = openai_to_anthropic(_FAKE_OPENAI_RESPONSE, "test-model")
        assert result["type"] == "message"

    def test_model_propagated(self):
        result = openai_to_anthropic(_FAKE_OPENAI_RESPONSE, "my-resolved-model")
        assert result["model"] == "my-resolved-model"

    def test_finish_reason_stop_to_end_turn(self):
        result = openai_to_anthropic(_FAKE_OPENAI_RESPONSE, "m")
        assert result["stop_reason"] == "end_turn"

    def test_finish_reason_length_to_max_tokens(self):
        data = {
            **_FAKE_OPENAI_RESPONSE,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "cut"},
                    "finish_reason": "length",
                }
            ],
        }
        result = openai_to_anthropic(data, "m")
        assert result["stop_reason"] == "max_tokens"

    def test_finish_reason_tool_calls_to_tool_use(self):
        data = {
            **_FAKE_OPENAI_RESPONSE,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": "tool_calls",
                }
            ],
        }
        result = openai_to_anthropic(data, "m")
        assert result["stop_reason"] == "tool_use"

    def test_unknown_finish_reason_falls_back_to_end_turn(self):
        data = {
            **_FAKE_OPENAI_RESPONSE,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "x"},
                    "finish_reason": "content_filter",
                }
            ],
        }
        result = openai_to_anthropic(data, "m")
        assert result["stop_reason"] == "end_turn"

    def test_usage_mapped(self):
        result = openai_to_anthropic(_FAKE_OPENAI_RESPONSE, "m")
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 7

    def test_missing_choices_safe(self):
        result = openai_to_anthropic({"id": "x"}, "m")
        assert result["content"] == [{"type": "text", "text": ""}]
        assert result["stop_reason"] == "end_turn"

    def test_missing_usage_safe(self):
        data = {**_FAKE_OPENAI_RESPONSE}
        data.pop("usage", None)
        result = openai_to_anthropic(data, "m")
        assert result["usage"]["input_tokens"] == 0
        assert result["usage"]["output_tokens"] == 0

    def test_id_propagated(self):
        result = openai_to_anthropic(_FAKE_OPENAI_RESPONSE, "m")
        assert result["id"] == "chatcmpl-test123"

    def test_missing_id_safe(self):
        result = openai_to_anthropic({}, "m")
        assert result["id"] == "msg_unknown"


# ─── 3. ollama_to_openai ──────────────────────────────────────────────────────


class TestOllamaToOpenAI:
    def test_basic_roundtrip(self):
        body = {
            "model": "llama3",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = ollama_to_openai(body)
        assert result["model"] == "llama3"
        assert result["messages"] == [{"role": "user", "content": "Hello"}]
        assert result["stream"] is False

    def test_options_temperature_mapped(self):
        body = {
            "model": "llama3",
            "messages": [],
            "options": {"temperature": 0.5},
        }
        result = ollama_to_openai(body)
        assert result["temperature"] == 0.5

    def test_options_num_predict_to_max_tokens(self):
        body = {
            "model": "llama3",
            "messages": [],
            "options": {"num_predict": 256},
        }
        result = ollama_to_openai(body)
        assert result["max_tokens"] == 256

    def test_no_options_no_temperature(self):
        body = {"model": "llama3", "messages": []}
        result = ollama_to_openai(body)
        assert "temperature" not in result

    def test_stream_passed_through(self):
        body = {"model": "llama3", "messages": [], "stream": True}
        result = ollama_to_openai(body)
        assert result["stream"] is True

    def test_empty_messages_safe(self):
        body = {"model": "llama3"}
        result = ollama_to_openai(body)
        assert result["messages"] == []


# ─── 4. openai_to_ollama ──────────────────────────────────────────────────────


class TestOpenAIToOllama:
    def test_basic_roundtrip(self):
        result = openai_to_ollama(_FAKE_OPENAI_RESPONSE, "llama3")
        assert result["model"] == "llama3"
        assert result["message"] == {"role": "assistant", "content": "Hello from the proxy!"}
        assert result["done"] is True

    def test_usage_mapped(self):
        result = openai_to_ollama(_FAKE_OPENAI_RESPONSE, "llama3")
        assert result["prompt_eval_count"] == 10
        assert result["eval_count"] == 7

    def test_missing_choices_safe(self):
        result = openai_to_ollama({}, "llama3")
        assert result["message"]["content"] == ""
        assert result["done"] is True

    def test_missing_usage_safe(self):
        data = {**_FAKE_OPENAI_RESPONSE}
        data.pop("usage", None)
        result = openai_to_ollama(data, "llama3")
        assert result["prompt_eval_count"] == 0
        assert result["eval_count"] == 0


# ─── 5. Endpoint integration tests ────────────────────────────────────────────


@pytest.fixture()
def client_with_fake_provider(client, monkeypatch):
    """Extends the standard `client` fixture by patching provider_registry so
    the compat endpoints get a real (but fake) OpenAI response without touching
    any backend.
    """
    import app.main

    monkeypatch.setattr(
        app.main.provider_registry,
        "chat_completions",
        _fake_chat_completions,
    )
    return client


class TestAnthropicEndpoint:
    def test_returns_200_anthropic_shape(self, client_with_fake_provider):
        response = client_with_fake_provider.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-opus",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert isinstance(body["content"], list)
        assert body["content"][0]["type"] == "text"
        assert body["content"][0]["text"] == "Hello from the proxy!"
        assert body["usage"]["input_tokens"] == 10
        assert body["usage"]["output_tokens"] == 7

    def test_compat_header_set(self, client_with_fake_provider):
        response = client_with_fake_provider.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-opus",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 200
        assert response.headers.get("x-orkoprox-compat") == "anthropic"

    def test_system_string_translated(self, client_with_fake_provider):
        """Verifies the endpoint accepts a top-level system string (Anthropic shape)."""
        response = client_with_fake_provider.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-opus",
                "system": "You are a pirate.",
                "messages": [{"role": "user", "content": "Arrr?"}],
            },
        )
        assert response.status_code == 200

    def test_stream_true_returns_400(self, client_with_fake_provider):
        response = client_with_fake_provider.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-opus",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "streaming_not_supported"

    def test_missing_auth_returns_401(self, client_with_fake_provider):
        response = client_with_fake_provider.post(
            "/v1/messages",
            json={
                "model": "claude-opus",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 401


class TestOllamaEndpoint:
    def test_returns_200_ollama_shape(self, client_with_fake_provider):
        response = client_with_fake_provider.post(
            "/api/chat",
            headers={"x-api-key": "test-key"},
            json={
                "model": "llama3",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["model"] == "test-model"
        assert body["message"]["role"] == "assistant"
        assert body["message"]["content"] == "Hello from the proxy!"
        assert body["done"] is True
        assert body["prompt_eval_count"] == 10
        assert body["eval_count"] == 7

    def test_compat_header_set(self, client_with_fake_provider):
        response = client_with_fake_provider.post(
            "/api/chat",
            headers={"x-api-key": "test-key"},
            json={
                "model": "llama3",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 200
        assert response.headers.get("x-orkoprox-compat") == "ollama"

    def test_options_temperature_accepted(self, client_with_fake_provider):
        response = client_with_fake_provider.post(
            "/api/chat",
            headers={"x-api-key": "test-key"},
            json={
                "model": "llama3",
                "messages": [{"role": "user", "content": "Hi"}],
                "options": {"temperature": 0.3},
            },
        )
        assert response.status_code == 200

    def test_stream_true_returns_400(self, client_with_fake_provider):
        response = client_with_fake_provider.post(
            "/api/chat",
            headers={"x-api-key": "test-key"},
            json={
                "model": "llama3",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "streaming_not_supported"

    def test_missing_auth_returns_401(self, client_with_fake_provider):
        response = client_with_fake_provider.post(
            "/api/chat",
            json={
                "model": "llama3",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 401
