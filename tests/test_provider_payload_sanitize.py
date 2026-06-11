from __future__ import annotations

from app.providers.openai_compatible import OpenAICompatibleProvider


def test_long_message_content_is_not_clipped() -> None:
    """Content clipping was removed — verify long messages pass through unchanged."""
    provider = OpenAICompatibleProvider(
        name="baseten",
        base_url="https://example.test/v1",
        api_key="k",
        default_model="nvidia/Nemotron-120B-A12B",
        timeout_s=30,
    )
    long_content = "x" * 2600
    payload = {
        "model": "nvidia/Nemotron-120B-A12B",
        "messages": [
            {"role": "system", "content": "ok"},
            {"role": "user", "content": long_content},
        ],
    }

    sanitized = provider._sanitize_payload(payload)
    content = sanitized["messages"][1]["content"]
    assert isinstance(content, str)
    assert content == long_content


def test_tools_and_tool_choice_are_normalized_to_chat_completions_shape() -> None:
    provider = OpenAICompatibleProvider(
        name="openai",
        base_url="https://example.test/v1",
        api_key="k",
        default_model="gpt-test",
        timeout_s=30,
    )
    payload = {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [
            {
                "type": "function",
                "name": "exec_command",
                "description": "run command",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        "tool_choice": {"type": "function", "name": "exec_command"},
    }

    sanitized = provider._sanitize_payload(payload)

    assert sanitized["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "exec_command",
                "description": "run command",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert sanitized["tool_choice"] == {"type": "function", "function": {"name": "exec_command"}}


def _make_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="ovh",
        base_url="https://example.test/v1",
        api_key="k",
        default_model="Mistral-Small-3.2-24B-Instruct-2506",
        timeout_s=30,
    )


def test_response_format_legacy_schema_wraps_into_json_schema() -> None:
    """OpenAI-Spec-Drift 2024-08: alte Clients senden {"type":"json_schema","schema":...},
    OVH erzwingt {"type":"json_schema","json_schema":{"name","schema","strict"}}."""
    provider = _make_provider()
    inner = {"type": "object", "required": ["msg"], "properties": {"msg": {"type": "string"}}}
    payload = {
        "model": "Mistral-Small-3.2-24B-Instruct-2506",
        "messages": [{"role": "user", "content": "Sag Hallo."}],
        "response_format": {"type": "json_schema", "schema": inner},
    }

    sanitized = provider._sanitize_payload(payload)
    rf = sanitized["response_format"]
    assert rf["type"] == "json_schema"
    assert "json_schema" in rf
    assert "schema" not in rf  # alte Schreibweise raus
    assert rf["json_schema"]["schema"] == inner
    assert rf["json_schema"]["name"] == "response"
    assert rf["json_schema"]["strict"] is True


def test_response_format_modern_json_schema_passthrough_with_defaults() -> None:
    """Wenn schon korrekt verschachtelt, fehlende Defaults (name, strict) ergänzen."""
    provider = _make_provider()
    inner = {"type": "object", "properties": {"x": {"type": "integer"}}}
    payload = {
        "model": "Mistral-Small-3.2-24B-Instruct-2506",
        "messages": [{"role": "user", "content": "x"}],
        "response_format": {"type": "json_schema", "json_schema": {"schema": inner}},
    }

    sanitized = provider._sanitize_payload(payload)
    rf = sanitized["response_format"]
    assert rf["json_schema"]["schema"] == inner
    assert rf["json_schema"]["name"] == "response"
    assert rf["json_schema"]["strict"] is True


def test_response_format_json_object_unchanged() -> None:
    """{"type":"json_object"} ist OpenAI-Spec-konform, darf nicht angetastet werden."""
    provider = _make_provider()
    payload = {
        "model": "Mistral-Small-3.2-24B-Instruct-2506",
        "messages": [{"role": "user", "content": "x"}],
        "response_format": {"type": "json_object"},
    }
    sanitized = provider._sanitize_payload(payload)
    assert sanitized["response_format"] == {"type": "json_object"}
