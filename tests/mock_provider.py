"""Test-only mock provider that simulates LLM responses.

This is the TEST counterpart to StubProvider. StubProvider in production
always raises 503 (because if you reach it, all real providers have failed).
TestMockProvider returns deterministic fake responses for test assertions.

NEVER import this in production code — it lives under tests/ only.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator
from uuid import uuid4

from app.providers.base import BaseProvider, ProviderCapabilities, ProviderRequestContext


class TestMockProvider(BaseProvider):
    capabilities = ProviderCapabilities(
        supports_stream=True,
        supports_tools=True,
        supports_parallel_tool_calls=True,
        supports_response_format=True,
    )

    def __init__(self, name: str = "mock", message: str = "mock response"):
        self.name = name
        self._message = message
        self.supports_stream = True

    async def chat_completions(self, payload: dict[str, Any], ctx: ProviderRequestContext) -> dict[str, Any]:
        model = payload.get("model", "mock-model")
        text = f"[{self.name}] {self._message}"
        tools = payload.get("tools") or []
        if tools:
            parallel = bool(payload.get("parallel_tool_calls")) and len(tools) > 1
            selected_tools = tools if parallel else tools[:1]
            tool_calls = []
            for idx, tool in enumerate(selected_tools):
                tool_name = tool.get("name") or tool.get("function", {}).get("name") or f"mock_tool_{idx}"
                call_id = f"call_{uuid4().hex}"
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps({"cmd": f"echo mock {idx}"}, ensure_ascii=True),
                        },
                    }
                )
            return {
                "id": f"chatcmpl-{uuid4()}",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": tool_calls,
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        return {
            "id": f"chatcmpl-{uuid4()}",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        }

    async def chat_completions_stream(
        self,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> AsyncIterator[bytes]:
        model = payload.get("model", "mock-model")
        tools = payload.get("tools") or []
        if tools:
            parallel = bool(payload.get("parallel_tool_calls")) and len(tools) > 1
            selected_tools = tools if parallel else tools[:1]
            chunks = [
                {
                    "id": f"chatcmpl-{uuid4()}",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": idx,
                                        "id": f"call_{uuid4().hex}",
                                        "type": "function",
                                        "function": {
                                            "name": tool.get("name") or tool.get("function", {}).get("name") or f"mock_tool_{idx}",
                                            "arguments": '{"cmd":"',
                                        },
                                    }
                                    for idx, tool in enumerate(selected_tools)
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": f"chatcmpl-{uuid4()}",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": idx,
                                        "function": {"arguments": f'echo mock {idx}"' + "}"},
                                    }
                                    for idx, _tool in enumerate(selected_tools)
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            ]
            for chunk in chunks:
                yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
            return
        chunks = [
            {
                "id": f"chatcmpl-{uuid4()}",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"[{self.name}] "}, "finish_reason": None}],
            },
            {
                "id": f"chatcmpl-{uuid4()}",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": self._message}, "finish_reason": "stop"}],
            },
        ]
        for chunk in chunks:
            yield f"data: {json.dumps(chunk, ensure_ascii=True)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    async def embeddings(
        self,
        payload: dict[str, Any],
        ctx: ProviderRequestContext,
    ) -> dict[str, Any]:
        model = payload.get("model", "mock-embedding-model")
        raw_input = payload.get("input", "")
        items = raw_input if isinstance(raw_input, list) else [raw_input]
        data = []
        for index, item in enumerate(items):
            seed = len(str(item or ""))
            data.append(
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": [float(seed), 0.0, 1.0],
                }
            )
        return {
            "object": "list",
            "data": data,
            "model": model,
            "usage": {"prompt_tokens": sum(len(str(item or "")) for item in items), "total_tokens": sum(len(str(item or "")) for item in items)},
        }
