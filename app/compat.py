"""app/compat.py — Wire-format translation layer (F7).

Pure dict-in / dict-out functions — no FastAPI imports, no I/O.
Translates between the Anthropic Messages API and Ollama /api/chat wire shapes
and the OpenAI Chat Completions format that orkoprox uses internally.

Scope (v0.1):
  - Non-streaming only.  stream=True requests are rejected at the endpoint layer
    with HTTP 400; streaming translation is deferred to a later iteration.
  - Tool-use: simple pass-through for Anthropic tools → OpenAI tools.  Complex
    multi-block tool_result content is handled best-effort (text parts extracted).
  - Image / audio content blocks are stripped to their text parts; callers that
    need multimodal pass-through should use the native /v1/chat/completions
    endpoint directly.
"""

from __future__ import annotations

from typing import Any


# ─── helpers ──────────────────────────────────────────────────────────────────


def _content_blocks_to_str(content: Any) -> str:
    """Collapse an Anthropic content array to a plain string.

    Handles:
      - str  → returned as-is
      - list of {"type": "text", "text": "..."}  → joined with newlines
      - list with mixed types  → text parts joined, non-text parts skipped
      - None / unexpected → ""
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(str(text))
        # image / tool_result / document blocks: skip (non-text, can't flatten)
    return "\n".join(parts)


_FINISH_REASON_TO_ANTHROPIC: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}


# ─── Anthropic ↔ OpenAI ───────────────────────────────────────────────────────


def anthropic_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic Messages-API request dict to an OpenAI
    Chat Completions request dict.

    Anthropic wire shape (relevant fields):
      {
        "model": str,
        "system": str | list[{"type":"text","text":str}],   # optional
        "messages": [{"role": "user"|"assistant", "content": str | list}],
        "max_tokens": int,
        "temperature": float,       # optional
        "stream": bool,             # optional, default false
        "tools": [...],             # optional, passed through as-is
      }

    Tool-use scope note: Anthropic tools use a slightly different schema
    ({"name", "description", "input_schema"}) vs OpenAI
    ({"type":"function","function":{"name","description","parameters"}}).
    Simple cases are remapped; complex or already-OpenAI-shaped tools pass
    through unchanged.  The compat endpoint is best-effort for tool use —
    callers with complex tool schemas should use /v1/chat/completions directly.
    """
    messages: list[dict[str, Any]] = []

    # Anthropic top-level system prompt → OpenAI system message
    raw_system = body.get("system")
    if raw_system:
        system_text = _content_blocks_to_str(raw_system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    # Translate conversation messages
    for msg in body.get("messages") or []:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        messages.append({"role": role, "content": _content_blocks_to_str(content)})

    out: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": messages,
    }
    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    # stream defaults to False; pass through so the endpoint can reject True
    out["stream"] = bool(body.get("stream", False))

    # Tools: remap Anthropic {name, description, input_schema} →
    # OpenAI {type:"function", function:{name, description, parameters}}
    # Pass through anything that already looks like an OpenAI tool.
    raw_tools = body.get("tools")
    if raw_tools:
        translated_tools: list[dict[str, Any]] = []
        for tool in raw_tools:
            if not isinstance(tool, dict):
                continue
            if "type" in tool and tool.get("type") == "function":
                # Already OpenAI-shaped — pass through unchanged
                translated_tools.append(tool)
            elif "name" in tool and "input_schema" in tool:
                # Anthropic native tool schema
                fn: dict[str, Any] = {"name": tool["name"]}
                if "description" in tool:
                    fn["description"] = tool["description"]
                fn["parameters"] = tool.get("input_schema") or {}
                translated_tools.append({"type": "function", "function": fn})
            else:
                # Unknown shape — pass through best-effort
                translated_tools.append(tool)
        if translated_tools:
            out["tools"] = translated_tools

    return out


def openai_to_anthropic(data: dict[str, Any], model: str) -> dict[str, Any]:
    """Translate an OpenAI Chat Completion response dict to an Anthropic
    Messages-API response dict.

    OpenAI response shape (relevant fields):
      {
        "id": str,
        "choices": [{"message": {"content": str|None, "tool_calls": [...]},
                     "finish_reason": str}],
        "usage": {"prompt_tokens": int, "completion_tokens": int},
      }

    Anthropic response shape returned:
      {
        "id": str,
        "type": "message",
        "role": "assistant",
        "model": str,
        "content": [{"type": "text", "text": str}],
        "stop_reason": "end_turn" | "max_tokens" | "tool_use",
        "usage": {"input_tokens": int, "output_tokens": int},
      }
    """
    resp_id: str = data.get("id") or "msg_unknown"

    choices: list[Any] = data.get("choices") or []
    message: dict[str, Any] = {}
    finish_reason_raw = "stop"
    if choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message") or {}
            finish_reason_raw = first.get("finish_reason") or "stop"

    content_text: str = message.get("content") or ""
    content_blocks: list[dict[str, Any]] = [{"type": "text", "text": content_text}]

    stop_reason = _FINISH_REASON_TO_ANTHROPIC.get(finish_reason_raw, "end_turn")

    raw_usage: dict[str, Any] = data.get("usage") or {}
    usage = {
        "input_tokens": int(raw_usage.get("prompt_tokens") or 0),
        "output_tokens": int(raw_usage.get("completion_tokens") or 0),
    }

    return {
        "id": resp_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "usage": usage,
    }


# ─── Ollama ↔ OpenAI ──────────────────────────────────────────────────────────


def ollama_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Ollama /api/chat request dict to an OpenAI Chat Completions
    request dict.

    Ollama wire shape:
      {
        "model": str,
        "messages": [{"role": str, "content": str}],
        "stream": bool,             # optional, default false
        "options": {"temperature": float, ...},   # optional
      }

    Ollama messages use the same role/content shape as OpenAI, so messages are
    passed through unchanged.
    """
    options: dict[str, Any] = body.get("options") or {}
    out: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": list(body.get("messages") or []),
        "stream": bool(body.get("stream", False)),
    }
    temperature = options.get("temperature")
    if temperature is not None:
        out["temperature"] = float(temperature)
    # Other Ollama options (num_predict → max_tokens, seed, top_k, top_p, …)
    # are not translated in v0.1 — extend here if needed.
    if "num_predict" in options:
        out["max_tokens"] = int(options["num_predict"])
    return out


def openai_to_ollama(data: dict[str, Any], model: str) -> dict[str, Any]:
    """Translate an OpenAI Chat Completion response dict to an Ollama
    /api/chat response dict.

    Ollama /api/chat response shape:
      {
        "model": str,
        "message": {"role": "assistant", "content": str},
        "done": true,
        "prompt_eval_count": int,
        "eval_count": int,
      }
    """
    choices: list[Any] = data.get("choices") or []
    content = ""
    if choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message") or {}
            content = msg.get("content") or ""

    raw_usage: dict[str, Any] = data.get("usage") or {}
    return {
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": True,
        "prompt_eval_count": int(raw_usage.get("prompt_tokens") or 0),
        "eval_count": int(raw_usage.get("completion_tokens") or 0),
    }
