"""Tests for the reasoning-field lift.

Some reasoning models return HTTP 200 with `message.reasoning` (or
`message.reasoning_content`) populated but `message.content=""`. Treating that
as `empty_content` would trigger an unnecessary fallback + provider cooldown,
so consumers would see `502 provider_cooldown_active` even though the provider
actually answered.

This lift copies the reasoning tokens into `content` so consumers can read
consistently from `message.content`, regardless of which field the provider
populated.
"""

from __future__ import annotations

from app.providers.router import ProviderRegistry


def _make_response(*, content: str | None, reasoning: str | None = None,
                   reasoning_content: str | None = None,
                   tool_calls: list | None = None) -> dict:
    """Hilfs-Builder fuer OpenAI-kompatible Chat-Completion-Response."""
    message: dict = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if reasoning is not None:
        message["reasoning"] = reasoning
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }


# ─── Lift-Verhalten ────────────────────────────────────────────────────


def test_lift_no_op_when_content_is_filled():
    """Wenn content schon gefuellt: kein Lift, keine Aenderung."""
    data = _make_response(content="Pong.", reasoning="ignored")
    changed = ProviderRegistry._lift_reasoning_to_content(data)
    assert changed is False
    assert data["choices"][0]["message"]["content"] == "Pong."


def test_lift_from_reasoning_when_content_empty():
    """OVH-Bug-Pattern: content="", reasoning="Pong." → content gefuellt."""
    data = _make_response(content="", reasoning="Pong.")
    changed = ProviderRegistry._lift_reasoning_to_content(data)
    assert changed is True
    assert data["choices"][0]["message"]["content"] == "Pong."
    # reasoning bleibt fuer Telemetrie erhalten
    assert data["choices"][0]["message"]["reasoning"] == "Pong."


def test_lift_from_reasoning_content_when_content_empty():
    """Variant: content="", reasoning_content="Pong." → content gefuellt."""
    data = _make_response(content="", reasoning_content="Hello world.")
    changed = ProviderRegistry._lift_reasoning_to_content(data)
    assert changed is True
    assert data["choices"][0]["message"]["content"] == "Hello world."


def test_lift_prefers_reasoning_content_over_reasoning():
    """Beide Felder gesetzt: reasoning_content gewinnt (OpenAI-spec-naeher)."""
    data = _make_response(content="", reasoning="A", reasoning_content="B")
    changed = ProviderRegistry._lift_reasoning_to_content(data)
    assert changed is True
    assert data["choices"][0]["message"]["content"] == "B"


def test_lift_from_content_null():
    """content=None statt "" — gleicher Fix-Pfad."""
    data = _make_response(content=None, reasoning="Pong.")
    changed = ProviderRegistry._lift_reasoning_to_content(data)
    assert changed is True
    assert data["choices"][0]["message"]["content"] == "Pong."


def test_lift_strips_think_block_from_qwen():
    """Qwen-Style: <think>...</think>actual answer → strip wrapper."""
    raw = "<think>\nLet me think about this.\n</think>\nPong."
    data = _make_response(content="", reasoning=raw)
    changed = ProviderRegistry._lift_reasoning_to_content(data)
    assert changed is True
    assert data["choices"][0]["message"]["content"] == "Pong."


def test_lift_handles_unclosed_think_tag():
    """Defensive: <think> ohne </think> → content bekommt was nach <think> kommt."""
    raw = "<think>Just thinking out loud Pong."
    data = _make_response(content="", reasoning=raw)
    changed = ProviderRegistry._lift_reasoning_to_content(data)
    assert changed is True
    assert data["choices"][0]["message"]["content"] == "Just thinking out loud Pong."


def test_lift_no_op_when_tool_calls_present():
    """Tool-Call-Antwort ist legitim ohne content — kein Lift."""
    data = _make_response(
        content="",
        reasoning="ignored",
        tool_calls=[{"id": "x", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
    )
    changed = ProviderRegistry._lift_reasoning_to_content(data)
    assert changed is False


def test_lift_no_op_when_reasoning_only_whitespace():
    """Reasoning-Feld nur mit Whitespace zaehlt nicht als gefuellt."""
    data = _make_response(content="", reasoning="   \n\n  ")
    changed = ProviderRegistry._lift_reasoning_to_content(data)
    assert changed is False


def test_lift_no_op_when_no_choices():
    """Defensive: leere/fehlende choices → kein Crash."""
    data: dict = {"choices": []}
    changed = ProviderRegistry._lift_reasoning_to_content(data)
    assert changed is False
    data2: dict = {}
    changed2 = ProviderRegistry._lift_reasoning_to_content(data2)
    assert changed2 is False


# ─── Empty-Content-Detection nach Lift ─────────────────────────────────


def test_is_empty_content_false_after_lift():
    """Pipeline-Garantie: Lift → is_empty_content liefert False."""
    data = _make_response(content="", reasoning="Pong.")
    ProviderRegistry._lift_reasoning_to_content(data)
    assert ProviderRegistry._is_empty_content(data) is False


def test_is_empty_content_true_when_no_reasoning_either():
    """Wenn weder content noch reasoning gefuellt sind: empty bleibt empty."""
    data = _make_response(content="")
    ProviderRegistry._lift_reasoning_to_content(data)
    assert ProviderRegistry._is_empty_content(data) is True
