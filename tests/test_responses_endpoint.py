"""Unit-Tests für den /v1/responses-Endpoint (gegen den mock-Provider).

Die Live-Compat-Matrix (tests/test_live_compat_matrix.py) deckt dieselbe API
gegen echte Provider ab; diese Datei verifiziert die Format-Übersetzung,
Tool-Call-Durchreichung, Streaming-Event-Sequenz und den Server-State
deterministisch ohne Netz.
"""

from __future__ import annotations

import json

MODEL = "mock/test-model"
HEADERS = {"x-api-key": "test-key"}

TOOL = {
    "type": "function",
    "name": "exec_command",
    "description": "Run a shell command.",
    "parameters": {
        "type": "object",
        "properties": {"cmd": {"type": "string"}},
        "required": ["cmd"],
        "additionalProperties": False,
    },
}


def _sse_events(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            continue
        events.append(json.loads(data))
    return events


# ─── Non-Streaming ──────────────────────────────────────────────────────────


def test_responses_non_stream_basic(client):
    response = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": MODEL, "input": "hallo", "stream": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert "output_text" in body
    assert isinstance(body["output"], list)
    # Mock-Provider liefert Text → genau ein message-Item.
    message_items = [i for i in body["output"] if i["type"] == "message"]
    assert len(message_items) == 1
    assert body["output_text"]


def test_responses_instructions_become_system(client):
    response = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={
            "model": MODEL,
            "instructions": "Be terse.",
            "input": "hi",
            "stream": False,
        },
    )
    assert response.status_code == 200
    response_id = response.json()["id"]
    items = client.get(f"/v1/responses/{response_id}/input_items", headers=HEADERS)
    assert items.status_code == 200
    roles = [item["role"] for item in items.json()["data"]]
    assert roles == ["system", "user"]


def test_responses_non_stream_tool_call(client):
    response = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={
            "model": MODEL,
            "input": "Call exec_command.",
            "tools": [TOOL],
            "tool_choice": {"type": "function", "name": "exec_command"},
            "parallel_tool_calls": False,
            "stream": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    function_call = next(i for i in body["output"] if i["type"] == "function_call")
    assert function_call["name"] == "exec_command"
    assert function_call["call_id"]
    # Argumente sind ein JSON-String.
    assert json.loads(function_call["arguments"])


# ─── Streaming ──────────────────────────────────────────────────────────────


def test_responses_stream_text(client):
    with client.stream(
        "POST",
        "/v1/responses",
        headers=HEADERS,
        json={"model": MODEL, "input": "stream me", "stream": True},
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    events = _sse_events(text)
    types = [e["type"] for e in events]
    assert "response.created" in types
    assert "response.output_text.delta" in types
    assert "response.output_item.done" in types
    assert "response.completed" in types
    assert "[DONE]" in text


def test_responses_stream_tool_call(client):
    with client.stream(
        "POST",
        "/v1/responses",
        headers=HEADERS,
        json={
            "model": MODEL,
            "input": "Call exec_command.",
            "tools": [TOOL],
            "tool_choice": {"type": "function", "name": "exec_command"},
            "parallel_tool_calls": False,
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    events = _sse_events(text)
    types = [e["type"] for e in events]
    assert "response.created" in types
    assert "response.output_item.added" in types
    assert "response.function_call_arguments.delta" in types
    assert "response.output_item.done" in types
    assert "response.completed" in types

    added = next(
        e
        for e in events
        if e["type"] == "response.output_item.added"
        and e["item"]["type"] == "function_call"
    )
    done = next(
        e
        for e in events
        if e["type"] == "response.output_item.done"
        and e["item"]["type"] == "function_call"
    )
    assert added["item"]["name"] == "exec_command"
    assert done["item"]["name"] == "exec_command"
    # Arguments-Akkumulation ergibt gültiges JSON.
    assert json.loads(done["item"]["arguments"])


# ─── Server-State: store / previous_response_id / retrieval ────────────────


def test_responses_store_and_retrieve(client):
    created = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": MODEL, "input": "merke dir X", "stream": False, "store": True},
    )
    assert created.status_code == 200
    response_id = created.json()["id"]

    fetched = client.get(f"/v1/responses/{response_id}", headers=HEADERS)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == response_id


def test_responses_store_false_not_retrievable(client):
    created = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": MODEL, "input": "fluechtig", "stream": False, "store": False},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["store"] is False
    fetched = client.get(f"/v1/responses/{body['id']}", headers=HEADERS)
    assert fetched.status_code == 404


def test_responses_previous_response_id_chains_context(client):
    first = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": MODEL, "input": "erste runde", "stream": False},
    )
    assert first.status_code == 200
    first_id = first.json()["id"]

    second = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={
            "model": MODEL,
            "previous_response_id": first_id,
            "input": "zweite runde",
            "stream": False,
        },
    )
    assert second.status_code == 200
    assert second.json()["previous_response_id"] == first_id


def test_responses_input_items_endpoint(client):
    created = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": MODEL, "input": "nur user", "stream": False},
    )
    response_id = created.json()["id"]
    items = client.get(f"/v1/responses/{response_id}/input_items", headers=HEADERS)
    assert items.status_code == 200
    body = items.json()
    assert body["object"] == "list"
    assert [item["role"] for item in body["data"]] == ["user"]


# ─── List / Delete ──────────────────────────────────────────────────────────


def test_responses_list_and_delete(client):
    created = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={"model": MODEL, "input": "listbar", "stream": False},
    )
    response_id = created.json()["id"]

    listed = client.get("/v1/responses", headers=HEADERS, params={"limit": 50})
    assert listed.status_code == 200
    listed_body = listed.json()
    assert listed_body["object"] == "list"
    assert any(item["id"] == response_id for item in listed_body["data"])

    deleted = client.delete(f"/v1/responses/{response_id}", headers=HEADERS)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    fetched = client.get(f"/v1/responses/{response_id}", headers=HEADERS)
    assert fetched.status_code == 404


def test_responses_delete_unknown_is_404(client):
    deleted = client.delete("/v1/responses/resp_doesnotexist", headers=HEADERS)
    assert deleted.status_code == 404


def test_responses_get_unknown_is_404(client):
    fetched = client.get("/v1/responses/resp_doesnotexist", headers=HEADERS)
    assert fetched.status_code == 404


# ─── Tool-Loop: function_call_output zurück in den Input ────────────────────


def test_responses_function_call_output_loop(client):
    first = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={
            "model": MODEL,
            "input": "Call exec_command.",
            "tools": [TOOL],
            "stream": False,
        },
    )
    assert first.status_code == 200
    function_call = next(
        i for i in first.json()["output"] if i["type"] == "function_call"
    )
    call_id = function_call["call_id"]

    second = client.post(
        "/v1/responses",
        headers=HEADERS,
        json={
            "model": MODEL,
            "previous_response_id": first.json()["id"],
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": {"result": "TOKEN_ZEBRA"},
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Repeat the token."}],
                },
            ],
            "stream": False,
        },
    )
    # Mock-Provider antwortet generisch — entscheidend ist, dass die
    # function_call_output→tool-Message-Übersetzung kein 4xx/5xx auslöst.
    assert second.status_code == 200
    assert second.json()["object"] == "response"


# ─── Auth ───────────────────────────────────────────────────────────────────


def test_responses_requires_auth(client):
    response = client.post(
        "/v1/responses",
        json={"model": MODEL, "input": "hi", "stream": False},
    )
    assert response.status_code in (401, 403)
