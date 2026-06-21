"""OpenAI Responses-API (``/v1/responses``) — Adapter über Chat-Completions.

Der Responses-Endpoint dupliziert NICHT die Guard-/Eskalations-/Cache-/Hook-
Pipeline aus ``chat_completions``. Stattdessen:

1. ``responses_request_to_chat`` übersetzt einen ``ResponsesRequest`` (inkl.
   ``instructions``, ``input`` als String/Item-Liste, ``previous_response_id``-
   Verkettung und ``function_call_output``-Tool-Loop) in einen
   ``ChatCompletionsRequest``.
2. Die Route ruft die *bestehende* ``chat_completions``-Funktion auf — damit
   laufen Pre-/Post-Guard, F8-Eskalationskaskade, Semantic-Cache und F6-Hooks
   unverändert.
3. ``chat_response_to_responses`` übersetzt die OpenAI-Chat-Completion-Antwort
   zurück ins Responses-Format (``output`` mit ``message``- und
   ``function_call``-Items, ``output_text``).
4. ``translate_chat_stream_to_responses`` übersetzt den Chat-Completions-SSE-
   Stream in die Responses-Event-Sequenz (``response.created``,
   ``response.output_item.added``, ``response.function_call_arguments.delta``,
   ``response.output_text.delta``, ``response.output_item.done``,
   ``response.completed``).

Server-State (``store``, ``previous_response_id``, Retrieval, List, Delete)
liegt im ``ResponseStore`` auf dem vorhandenen ``KeyValueStore``-Protokoll.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, model_validator

from app.storage import KeyValueStore

# ─── Schemas ────────────────────────────────────────────────────────────────


class ResponsesRequest(BaseModel):
    """OpenAI-Responses-API-Request.

    ``input`` ist entweder ein einfacher Prompt-String oder eine Liste von
    Items (``message`` mit ``role``/``content``-Parts, ``function_call_output``
    aus einem vorigen Tool-Call). ``instructions`` wird zur System-Message.
    ``previous_response_id`` verkettet mit einer gespeicherten Vorgänger-
    Antwort (deren Input + Output der neuen Anfrage vorangestellt werden).
    """

    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[dict[str, Any]] | None = None
    instructions: str | None = None
    previous_response_id: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    stream: bool = False
    store: bool = True
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_input_present(self) -> "ResponsesRequest":
        if self.input is None and self.previous_response_id is None:
            raise ValueError("input is required unless previous_response_id is set")
        return self


# ─── Input-Übersetzung: Responses → Chat-Completions ────────────────────────


def _content_parts_to_text(content: Any) -> str | list[dict[str, Any]]:
    """Übersetzt Responses-content-Parts in Chat-Completions-content.

    Responses nutzt ``input_text``/``output_text``/``input_image``; Chat nutzt
    ``text``/``image_url``. Einfache Strings bleiben Strings.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    out: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            out.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            out.append({"type": "text", "text": part.get("text", "")})
        elif ptype in ("input_image", "image_url"):
            image_url = part.get("image_url") or part.get("image")
            if isinstance(image_url, str):
                image_url = {"url": image_url}
            out.append({"type": "image_url", "image_url": image_url})
        else:
            # Unbekannter Part-Typ: best-effort als Text durchreichen.
            text = part.get("text")
            if isinstance(text, str):
                out.append({"type": "text", "text": text})
    # Reiner Text → flachen String zurückgeben (Provider-kompatibler).
    if out and all(p["type"] == "text" for p in out):
        return "\n".join(p["text"] for p in out)
    return out


def _input_items_to_messages(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Übersetzt eine Responses-input-Item-Liste in Chat-Messages.

    Unterstützte Item-Typen:
    - ``message`` (role + content) → Chat-Message
    - ``function_call`` (vom Assistant) → assistant-Message mit ``tool_calls``
    - ``function_call_output`` (Tool-Ergebnis) → ``tool``-Message
    - Roh-Dict mit ``role`` (kein ``type``) → direkt als Message
    """
    messages: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "function_call_output":
            output = item.get("output")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("id") or "",
                    "content": output,
                }
            )
        elif itype == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": item.get("call_id") or item.get("id") or f"call_{uuid4().hex}",
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "{}"),
                            },
                        }
                    ],
                }
            )
        elif itype == "message" or "role" in item:
            messages.append(
                {
                    "role": item.get("role", "user"),
                    "content": _content_parts_to_text(item.get("content", "")),
                }
            )
    return messages


def build_messages(
    req: ResponsesRequest, prior_messages: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Baut die vollständige Chat-Message-Liste für eine Responses-Anfrage.

    Reihenfolge: ``instructions`` (System) → verkettete Vorgänger-Messages
    (aus ``previous_response_id``) → die aktuellen ``input``-Items.
    """
    messages: list[dict[str, Any]] = []
    if req.instructions:
        messages.append({"role": "system", "content": req.instructions})
    if prior_messages:
        messages.extend(prior_messages)
    if req.input is None:
        pass
    elif isinstance(req.input, str):
        messages.append({"role": "user", "content": req.input})
    else:
        messages.extend(_input_items_to_messages(req.input))
    if not messages:
        # Chat-Completions verlangt mindestens eine Message.
        messages.append({"role": "user", "content": ""})
    return messages


def responses_request_to_chat(
    req: ResponsesRequest, prior_messages: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Übersetzt ResponsesRequest → ChatCompletionsRequest-Payload (dict).

    ``tools`` werden vom Responses-Flat-Format (``{type, name, parameters}``)
    in das Chat-Nested-Format (``{type, function:{name, parameters}}``)
    konvertiert, falls nötig. ``tool_choice`` analog.
    """
    payload: dict[str, Any] = {
        "model": req.model,
        "messages": build_messages(req, prior_messages),
        "stream": req.stream,
    }
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.max_output_tokens is not None:
        payload["max_tokens"] = req.max_output_tokens
    if req.tools:
        payload["tools"] = [_tool_to_chat(tool) for tool in req.tools]
    if req.tool_choice is not None:
        payload["tool_choice"] = _tool_choice_to_chat(req.tool_choice)
    if req.parallel_tool_calls is not None:
        payload["parallel_tool_calls"] = req.parallel_tool_calls
    return payload


def _tool_to_chat(tool: dict[str, Any]) -> dict[str, Any]:
    """Responses-Tool (flach) → Chat-Tool (nested unter ``function``)."""
    if not isinstance(tool, dict):
        return tool
    # Bereits Chat-Format (hat verschachteltes "function")?
    if isinstance(tool.get("function"), dict):
        return tool
    if tool.get("type") == "function":
        function = {
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {}),
        }
        if tool.get("strict") is not None:
            function["strict"] = tool["strict"]
        return {"type": "function", "function": function}
    return tool


def _tool_choice_to_chat(choice: str | dict[str, Any]) -> str | dict[str, Any]:
    """Responses-tool_choice → Chat-tool_choice."""
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict) and choice.get("type") == "function":
        if isinstance(choice.get("function"), dict):
            return choice
        return {"type": "function", "function": {"name": choice.get("name", "")}}
    return choice


# ─── Output-Übersetzung: Chat-Completions → Responses ───────────────────────


def chat_message_to_output_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Übersetzt eine Chat-Completion-assistant-Message in Responses-output-Items.

    - ``content`` → ``message``-Item mit ``output_text``-Part
    - jeder ``tool_calls``-Eintrag → ``function_call``-Item
    """
    items: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        items.append(
            {
                "type": "message",
                "id": f"msg_{uuid4().hex}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        )
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        items.append(
            {
                "type": "function_call",
                "id": f"fc_{uuid4().hex}",
                "call_id": call.get("id") or f"call_{uuid4().hex}",
                "name": function.get("name", ""),
                "arguments": arguments,
                "status": "completed",
            }
        )
    return items


def extract_output_text(output_items: list[dict[str, Any]]) -> str:
    """Konkateniert alle ``output_text``-Parts der ``message``-Items."""
    parts: list[str] = []
    for item in output_items:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                parts.append(part.get("text", ""))
    return "".join(parts)


def chat_response_to_responses(
    data: dict[str, Any],
    req: ResponsesRequest,
    *,
    response_id: str,
    created_at: int,
    resolved_model: str,
) -> dict[str, Any]:
    """Baut das vollständige Responses-Objekt aus einer Chat-Completion."""
    message: dict[str, Any] = {}
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        message = {}
    output_items = chat_message_to_output_items(message)
    output_text = extract_output_text(output_items)
    usage = data.get("usage") or {}
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": resolved_model or req.model,
        "output": output_items,
        "output_text": output_text,
        "previous_response_id": req.previous_response_id,
        "store": req.store,
        "instructions": req.instructions,
        "tools": req.tools or [],
        "tool_choice": req.tool_choice if req.tool_choice is not None else "auto",
        "parallel_tool_calls": (
            req.parallel_tool_calls if req.parallel_tool_calls is not None else True
        ),
        "temperature": req.temperature,
        "max_output_tokens": req.max_output_tokens,
        "metadata": req.metadata or {},
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "error": None,
    }


# ─── Streaming-Übersetzung: Chat-SSE → Responses-SSE ────────────────────────


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=True)}\n\n".encode("utf-8")


def _iter_chat_data_lines(buffer: str, chunk_text: str) -> tuple[list[str], str]:
    collected: list[str] = []
    buffer += chunk_text
    while "\n\n" in buffer:
        raw_event, buffer = buffer.split("\n\n", 1)
        for line in raw_event.splitlines():
            if line.startswith("data:"):
                collected.append(line[5:].strip())
    return collected, buffer


async def translate_chat_stream_to_responses(
    chat_stream: AsyncIterable[bytes | str | memoryview],
    req: ResponsesRequest,
    *,
    response_id: str,
    created_at: int,
    resolved_model: str,
    on_complete: Any = None,
) -> AsyncIterator[bytes]:
    """Übersetzt den Chat-Completions-SSE-Stream in Responses-Events.

    Emittiert die OpenAI-Responses-Event-Sequenz:
    ``response.created`` → pro Output-Item ``response.output_item.added`` +
    Delta-Events (``response.output_text.delta`` /
    ``response.function_call_arguments.delta``) + ``response.output_item.done``
    → ``response.completed``.

    Tool-Calls werden über ihren ``index`` akkumuliert (Name + Arguments kommen
    in mehreren Chunks). ``on_complete`` (optional) wird mit dem finalen
    Responses-Objekt aufgerufen — für Server-State-Persistenz.
    """
    base_response = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "in_progress",
        "model": resolved_model or req.model,
        "output": [],
        "previous_response_id": req.previous_response_id,
        "store": req.store,
    }
    yield _sse({"type": "response.created", "response": dict(base_response)})

    sequence = 0
    output_index = 0
    # Text-Item-State.
    text_item_open = False
    text_item_id = ""
    text_accumulated = ""
    # Tool-Call-State, keyed by tool-call-index.
    tool_calls: dict[int, dict[str, Any]] = {}
    tool_order: list[int] = []
    final_output: list[dict[str, Any]] = []

    def _next_seq() -> int:
        nonlocal sequence
        sequence += 1
        return sequence

    buffer = ""
    async for chunk in chat_stream:
        # StreamingResponse.body_iterator kann str, bytes ODER memoryview liefern.
        if isinstance(chunk, str):
            text = chunk
        else:
            text = bytes(chunk).decode("utf-8", errors="ignore")
        data_lines, buffer = _iter_chat_data_lines(buffer, text)
        for data_line in data_lines:
            if not data_line or data_line == "[DONE]":
                continue
            try:
                parsed = json.loads(data_line)
            except (json.JSONDecodeError, ValueError):
                continue
            choices = parsed.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            # ── Text-Delta ──
            content_delta = delta.get("content")
            if isinstance(content_delta, str) and content_delta:
                if not text_item_open:
                    text_item_open = True
                    text_item_id = f"msg_{uuid4().hex}"
                    yield _sse(
                        {
                            "type": "response.output_item.added",
                            "output_index": output_index,
                            "sequence_number": _next_seq(),
                            "item": {
                                "type": "message",
                                "id": text_item_id,
                                "role": "assistant",
                                "status": "in_progress",
                                "content": [],
                            },
                        }
                    )
                text_accumulated += content_delta
                yield _sse(
                    {
                        "type": "response.output_text.delta",
                        "output_index": output_index,
                        "item_id": text_item_id,
                        "sequence_number": _next_seq(),
                        "delta": content_delta,
                    }
                )

            # ── Tool-Call-Deltas ──
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                if idx not in tool_calls:
                    # Text-Item schließen bevor ein Tool-Item beginnt.
                    if text_item_open:
                        yield _sse_text_done(
                            text_item_id, output_index, text_accumulated, _next_seq()
                        )
                        final_output.append(
                            _message_item(text_item_id, text_accumulated)
                        )
                        text_item_open = False
                        output_index += 1
                    function = tc.get("function") or {}
                    state = {
                        "output_index": output_index,
                        "fc_id": f"fc_{uuid4().hex}",
                        "call_id": tc.get("id") or f"call_{uuid4().hex}",
                        "name": function.get("name", "") or "",
                        "arguments": "",
                    }
                    tool_calls[idx] = state
                    tool_order.append(idx)
                    output_index += 1
                    yield _sse(
                        {
                            "type": "response.output_item.added",
                            "output_index": state["output_index"],
                            "sequence_number": _next_seq(),
                            "item": {
                                "type": "function_call",
                                "id": state["fc_id"],
                                "call_id": state["call_id"],
                                "name": state["name"],
                                "arguments": "",
                                "status": "in_progress",
                            },
                        }
                    )
                state = tool_calls[idx]
                function = tc.get("function") or {}
                if function.get("name"):
                    state["name"] = function["name"]
                arg_delta = function.get("arguments")
                if isinstance(arg_delta, str) and arg_delta:
                    state["arguments"] += arg_delta
                    yield _sse(
                        {
                            "type": "response.function_call_arguments.delta",
                            "output_index": state["output_index"],
                            "item_id": state["fc_id"],
                            "call_id": state["call_id"],
                            "sequence_number": _next_seq(),
                            "delta": arg_delta,
                        }
                    )

    # ── Stream-Ende: offene Items schließen ──
    if text_item_open:
        yield _sse_text_done(text_item_id, output_index, text_accumulated, _next_seq())
        final_output.append(_message_item(text_item_id, text_accumulated))

    for idx in tool_order:
        state = tool_calls[idx]
        item = {
            "type": "function_call",
            "id": state["fc_id"],
            "call_id": state["call_id"],
            "name": state["name"],
            "arguments": state["arguments"],
            "status": "completed",
        }
        yield _sse(
            {
                "type": "response.function_call_arguments.done",
                "output_index": state["output_index"],
                "item_id": state["fc_id"],
                "call_id": state["call_id"],
                "sequence_number": _next_seq(),
                "arguments": state["arguments"],
            }
        )
        yield _sse(
            {
                "type": "response.output_item.done",
                "output_index": state["output_index"],
                "sequence_number": _next_seq(),
                "item": item,
            }
        )
        final_output.append(item)

    completed = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": resolved_model or req.model,
        "output": final_output,
        "output_text": extract_output_text(final_output),
        "previous_response_id": req.previous_response_id,
        "store": req.store,
        "instructions": req.instructions,
        "metadata": req.metadata or {},
        "error": None,
    }
    yield _sse(
        {
            "type": "response.completed",
            "sequence_number": _next_seq(),
            "response": completed,
        }
    )
    yield b"data: [DONE]\n\n"
    if on_complete is not None:
        on_complete(completed)


def _message_item(item_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _sse_text_done(
    item_id: str, output_index: int, text: str, sequence: int
) -> bytes:
    return _sse(
        {
            "type": "response.output_item.done",
            "output_index": output_index,
            "sequence_number": sequence,
            "item": _message_item(item_id, text),
        }
    )


# ─── Server-State: ResponseStore ────────────────────────────────────────────


class ResponseStore:
    """Persistiert gespeicherte Responses (``store=true``) für Retrieval,
    Verkettung (``previous_response_id``), Listing und Delete.

    Liegt auf dem ``KeyValueStore``-Protokoll — in-memory (Zero-Config) oder
    Redis (multi-process). Gespeichert werden zwei Blobs pro Response:

    - ``resp:obj:{id}``   → das vollständige Responses-Objekt (JSON)
    - ``resp:msgs:{id}``  → die zugehörigen Chat-Messages (Input + Output),
      damit ein Folge-Request via ``previous_response_id`` die Historie
      rekonstruieren kann.

    Ein Index-Blob ``resp:index`` hält die ID-Reihenfolge für das Listing.
    """

    OBJ_PREFIX = "resp:obj:"
    MSGS_PREFIX = "resp:msgs:"
    INDEX_KEY = "resp:index"
    TTL_SECONDS = 7 * 24 * 3600  # 7 Tage (über incr_fields-TTL-Refresh nicht nötig)

    def __init__(self, store: KeyValueStore) -> None:
        self._store = store

    def save(
        self,
        response_obj: dict[str, Any],
        chat_messages: list[dict[str, Any]],
    ) -> None:
        response_id = response_obj["id"]
        self._store.set_str(f"{self.OBJ_PREFIX}{response_id}", json.dumps(response_obj))
        self._store.set_str(
            f"{self.MSGS_PREFIX}{response_id}", json.dumps(chat_messages)
        )
        self._append_index(response_id, response_obj.get("created_at", 0))

    def get(self, response_id: str) -> dict[str, Any] | None:
        raw = self._store.get_str(f"{self.OBJ_PREFIX}{response_id}")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    def get_messages(self, response_id: str) -> list[dict[str, Any]] | None:
        raw = self._store.get_str(f"{self.MSGS_PREFIX}{response_id}")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    def input_items(self, response_id: str) -> list[dict[str, Any]] | None:
        """Die Input-Messages (ohne den Assistant-Output) als Responses-
        ``input_items``-Liste — eine Message pro Eintrag mit ``role``+``content``.
        """
        obj = self.get(response_id)
        if obj is None:
            return None
        messages = self.get_messages(response_id) or []
        # Output-Items sind separat im Objekt; input_items = alle Messages, die
        # NICHT der finale Assistant-Output sind. Wir speichern in `save` nur die
        # Input-Messages unter MSGS (Output steckt im Objekt), daher: alle.
        items: list[dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            items.append(
                {
                    "id": f"msg_{response_id}_{idx}",
                    "type": "message",
                    "role": msg.get("role", "user"),
                    "content": _normalize_stored_content(msg.get("content")),
                }
            )
        return items

    def delete(self, response_id: str) -> bool:
        if self.get(response_id) is None:
            return False
        self._store.delete(f"{self.OBJ_PREFIX}{response_id}")
        self._store.delete(f"{self.MSGS_PREFIX}{response_id}")
        self._remove_from_index(response_id)
        return True

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        ids = self._read_index()
        # Neueste zuerst.
        ids = list(reversed(ids))[: max(0, limit)]
        out: list[dict[str, Any]] = []
        for response_id in ids:
            obj = self.get(response_id)
            if obj is not None:
                out.append(obj)
        return out

    # ── Index-Verwaltung (eigener String-Blob, da KeyValueStore kein List-Typ) ──

    def _read_index(self) -> list[str]:
        raw = self._store.get_str(self.INDEX_KEY)
        if raw is None:
            return []
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else []
        except (json.JSONDecodeError, ValueError):
            return []

    def _append_index(self, response_id: str, created_at: int) -> None:
        ids = self._read_index()
        if response_id not in ids:
            ids.append(response_id)
            self._store.set_str(self.INDEX_KEY, json.dumps(ids))

    def _remove_from_index(self, response_id: str) -> None:
        ids = self._read_index()
        if response_id in ids:
            ids = [i for i in ids if i != response_id]
            self._store.set_str(self.INDEX_KEY, json.dumps(ids))


def _normalize_stored_content(content: Any) -> list[dict[str, Any]]:
    """Stored Chat-content → Responses-input_items-content-Parts."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if isinstance(content, list):
        out: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append({"type": "input_text", "text": part.get("text", "")})
            elif isinstance(part, dict):
                out.append(part)
            elif isinstance(part, str):
                out.append({"type": "input_text", "text": part})
        return out
    return [{"type": "input_text", "text": str(content or "")}]


def reconstruct_prior_messages(
    store: ResponseStore, previous_response_id: str | None
) -> list[dict[str, Any]]:
    """Baut die Vorgänger-Chat-Messages für eine ``previous_response_id``-Kette.

    Nimmt die gespeicherten Input-Messages der Vorgänger-Response PLUS deren
    Assistant-Output (Text + Tool-Calls), damit das Modell den vollen Kontext
    der vorigen Runde sieht.
    """
    if not previous_response_id:
        return []
    prior = store.get_messages(previous_response_id) or []
    result = list(prior)
    obj = store.get(previous_response_id)
    if obj is not None:
        for item in obj.get("output") or []:
            if item.get("type") == "message":
                text = extract_output_text([item])
                if text:
                    result.append({"role": "assistant", "content": text})
            elif item.get("type") == "function_call":
                result.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": item.get("call_id"),
                                "type": "function",
                                "function": {
                                    "name": item.get("name", ""),
                                    "arguments": item.get("arguments", "{}"),
                                },
                            }
                        ],
                    }
                )
    return result


def new_response_id() -> str:
    return f"resp_{uuid4().hex}"


def now_unix() -> int:
    return int(time.time())
