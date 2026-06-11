from __future__ import annotations

import json
import os
from collections.abc import Iterator

import httpx
import pytest


LIVE_COMPAT_BASE_URL = "LIVE_COMPAT_BASE_URL"
LIVE_COMPAT_API_KEY = "LIVE_COMPAT_API_KEY"
LIVE_COMPAT_TIMEOUT_S = "LIVE_COMPAT_TIMEOUT_S"

EXPECTED_TOOL_NAME = "exec_command"
EXPECTED_TOOL_ARGS = {"cmd": "pwd"}
MATRIX_MODELS = ("high", "gpt-5.4")


def _require_live_config() -> tuple[str, str, float]:
    api_key = os.getenv(LIVE_COMPAT_API_KEY, "").strip()
    if not api_key:
        pytest.skip(f"{LIVE_COMPAT_API_KEY} is not set")
    base_url = os.getenv(LIVE_COMPAT_BASE_URL, "http://localhost:8091").rstrip("/")
    timeout_s = float(os.getenv(LIVE_COMPAT_TIMEOUT_S, "90"))
    return base_url, api_key, timeout_s


@pytest.fixture(scope="module")
def live_client() -> Iterator[httpx.Client]:
    base_url, api_key, timeout_s = _require_live_config()
    with httpx.Client(
        base_url=base_url,
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        timeout=timeout_s,
    ) as client:
        yield client


def _tool_request_payload(model: str, *, stream: bool) -> dict[str, object]:
    return {
        "model": model,
        "instructions": "You must call exec_command exactly once.",
        "input": "Call exec_command with JSON arguments {\"cmd\":\"pwd\"}. Do not answer normally.",
        "tools": [
            {
                "type": "function",
                "name": EXPECTED_TOOL_NAME,
                "description": "Run a shell command.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cmd": {
                            "type": "string",
                            "description": "Shell command to execute.",
                        }
                    },
                    "required": ["cmd"],
                    "additionalProperties": False,
                },
            }
        ],
        "tool_choice": {"type": "function", "name": EXPECTED_TOOL_NAME},
        "parallel_tool_calls": False,
        "stream": stream,
    }


def _iter_sse_events(response: httpx.Response) -> Iterator[dict[str, object]]:
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            continue
        yield json.loads(data)


def _basic_response_payload(model: str, *, store: bool = True) -> dict[str, object]:
    return {
        "model": model,
        "instructions": "Reply tersely.",
        "input": "Merke dir das Wort BANANE und antworte nur ACK.",
        "max_output_tokens": 20,
        "stream": False,
        "store": store,
    }


@pytest.mark.parametrize("model", MATRIX_MODELS)
def test_live_models_endpoint_exposes_alias(model: str, live_client: httpx.Client) -> None:
    response = live_client.get("/v1/models")

    assert response.status_code == 200
    body = response.json()
    model_ids = {entry["id"] for entry in body["data"]}
    assert model in model_ids


@pytest.mark.parametrize("model", MATRIX_MODELS)
def test_live_responses_non_stream_tool_call_matrix(model: str, live_client: httpx.Client) -> None:
    response = live_client.post("/v1/responses", json=_tool_request_payload(model, stream=False))

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    function_call = next(item for item in body["output"] if item["type"] == "function_call")
    assert function_call["name"] == EXPECTED_TOOL_NAME
    assert json.loads(function_call["arguments"]) == EXPECTED_TOOL_ARGS


@pytest.mark.parametrize("model", MATRIX_MODELS)
def test_live_responses_stream_tool_call_matrix(model: str, live_client: httpx.Client) -> None:
    with live_client.stream(
        "POST",
        "/v1/responses",
        json=_tool_request_payload(model, stream=True),
    ) as response:
        assert response.status_code == 200
        events = list(_iter_sse_events(response))

    event_types = [event["type"] for event in events]
    assert "response.created" in event_types
    assert "response.output_item.added" in event_types
    assert "response.function_call_arguments.delta" in event_types
    assert "response.output_item.done" in event_types
    assert "response.completed" in event_types

    added = next(
        event
        for event in events
        if event["type"] == "response.output_item.added"
        and event["item"]["type"] == "function_call"
    )
    done = next(
        event
        for event in events
        if event["type"] == "response.output_item.done"
        and event["item"]["type"] == "function_call"
    )

    assert added["item"]["name"] == EXPECTED_TOOL_NAME
    assert done["item"]["name"] == EXPECTED_TOOL_NAME
    assert json.loads(done["item"]["arguments"]) == EXPECTED_TOOL_ARGS


@pytest.mark.parametrize("model", MATRIX_MODELS)
def test_live_responses_previous_response_id_and_retrieval_matrix(model: str, live_client: httpx.Client) -> None:
    first = live_client.post("/v1/responses", json=_basic_response_payload(model))

    assert first.status_code == 200
    first_body = first.json()
    assert first_body["output_text"] == "ACK"

    second = live_client.post(
        "/v1/responses",
        json={
            "model": model,
            "previous_response_id": first_body["id"],
            "input": "Welches Wort sollte ich mir merken? Antworte nur mit dem Wort.",
            "max_output_tokens": 20,
            "stream": False,
        },
    )

    assert second.status_code == 200
    second_body = second.json()
    assert second_body["output_text"] == "BANANE"
    assert second_body["previous_response_id"] == first_body["id"]

    fetched = live_client.get(f"/v1/responses/{first_body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == first_body["id"]

    input_items = live_client.get(f"/v1/responses/{first_body['id']}/input_items")
    assert input_items.status_code == 200
    input_items_body = input_items.json()
    assert input_items_body["object"] == "list"
    assert [item["role"] for item in input_items_body["data"]] == ["system", "user"]


@pytest.mark.parametrize("model", MATRIX_MODELS)
def test_live_responses_store_false_is_not_retrievable(model: str, live_client: httpx.Client) -> None:
    response = live_client.post("/v1/responses", json=_basic_response_payload(model, store=False))

    assert response.status_code == 200
    body = response.json()
    assert body["store"] is False

    fetched = live_client.get(f"/v1/responses/{body['id']}")
    assert fetched.status_code == 404


@pytest.mark.parametrize("model", MATRIX_MODELS)
def test_live_responses_list_and_delete_matrix(model: str, live_client: httpx.Client) -> None:
    created = live_client.post("/v1/responses", json=_basic_response_payload(model))

    assert created.status_code == 200
    response_id = created.json()["id"]

    listed = live_client.get("/v1/responses", params={"limit": 50})
    assert listed.status_code == 200
    listed_body = listed.json()
    assert any(item["id"] == response_id for item in listed_body["data"])

    deleted = live_client.delete(f"/v1/responses/{response_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    fetched = live_client.get(f"/v1/responses/{response_id}")
    assert fetched.status_code == 404


@pytest.mark.parametrize("model", MATRIX_MODELS)
def test_live_responses_function_call_output_tool_loop_matrix(model: str, live_client: httpx.Client) -> None:
    first = live_client.post("/v1/responses", json=_tool_request_payload(model, stream=False))

    assert first.status_code == 200
    first_body = first.json()
    function_call = next(item for item in first_body["output"] if item["type"] == "function_call")
    call_id = function_call["call_id"]

    second = live_client.post(
        "/v1/responses",
        json={
            "model": model,
            "previous_response_id": first_body["id"],
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": {"result": "A1B2C3_ZEBRA"},
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Repeat the tool result token exactly."}],
                },
            ],
            "max_output_tokens": 20,
            "stream": False,
        },
    )

    assert second.status_code == 200
    assert "A1B2C3_ZEBRA" in second.json()["output_text"]
