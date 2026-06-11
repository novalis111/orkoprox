from __future__ import annotations

import json

from app.providers.openai_compatible import OpenAICompatibleProvider


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="ovh",
        base_url="https://example.invalid/v1",
        api_key="x",
        default_model="m",
        timeout_s=5,
    )


def test_extract_openai_standard_error_format() -> None:
    """Format 1: {"error": {"message": ...}} (OpenAI-Standard)."""
    provider = _provider()
    details = {"error": {"message": "context length exceeded", "type": "invalid_request"}}
    assert provider._extract_error_message(details, 400) == "context length exceeded"


def test_extract_ovh_nested_message_string_format() -> None:
    """Format 2: {"message": "<json-string mit error-objekt>"} (OVH).

    Das ist der reale OVH-400-Body fuer Alias `high` bei zu langem Prompt.
    Beweisstueck aus der Hub-Diagnose (Direkt-Probe gegen OVH).
    """
    provider = _provider()
    nested = json.dumps(
        {
            "error": {
                "message": "max_tokens must be at least 1, got -28938. (parameter=max_tokens, value=-28938)",
                "type": "BadRequestError",
                "param": "max_tokens",
                "code": 400,
            }
        }
    )
    details = {"message": nested}
    assert provider._extract_error_message(details, 400) == (
        "max_tokens must be at least 1, got -28938. (parameter=max_tokens, value=-28938)"
    )


def test_extract_raw_message_string_format() -> None:
    """Format 3: {"message": "..."} ohne nested JSON -> roher String (truncated)."""
    provider = _provider()
    details = {"message": "Service temporarily unavailable"}
    assert provider._extract_error_message(details, 503) == "Service temporarily unavailable"


def test_extract_raw_message_string_is_truncated() -> None:
    """Roher message-String wird auf 500 Zeichen gekuerzt (Log-Flut-Schutz)."""
    provider = _provider()
    details = {"message": "x" * 2000}
    result = provider._extract_error_message(details, 400)
    assert len(result) == 500
    assert result == "x" * 500


def test_extract_fallback_when_no_message() -> None:
    """Fallback-Kette endet bei f'{name} error {status}' wenn nichts parsebar ist."""
    provider = _provider()
    assert provider._extract_error_message({}, 400) == "ovh error 400"
    assert provider._extract_error_message({"body": "<html>500</html>"}, 500) == "ovh error 500"


def test_extract_prefers_standard_error_over_message_key() -> None:
    """Wenn beide Keys da sind, gewinnt der OpenAI-Standard (error.message)."""
    provider = _provider()
    details = {"error": {"message": "primary"}, "message": "secondary"}
    assert provider._extract_error_message(details, 400) == "primary"


def test_extract_malformed_nested_message_falls_back_to_raw() -> None:
    """Top-Level message ist String, aber kein valides JSON -> roher String."""
    provider = _provider()
    details = {"message": "not-json-{broken"}
    assert provider._extract_error_message(details, 400) == "not-json-{broken"
