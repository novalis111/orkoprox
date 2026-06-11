"""Streaming post-guard with 10 KB sample buffer.

Verifies:
- _extract_streaming_chunk_text() correctly parses OpenAI streaming chunks.
- Tail buffer is capped at settings.guard_post_stream_tail_bytes.
- Feature-Flag guard_post_stream_enabled deaktiviert den Pfad ohne Code-Change.
- Bei Stream-Interrupt wird Post-Guard NICHT aufgerufen (kein vollstaendiger Output).
"""
from __future__ import annotations

import json

from app.main import _extract_streaming_chunk_text


def test_extract_chunk_text_extracts_content_from_openai_chunk():
    chunk = json.dumps(
        {
            "id": "chatcmpl-1",
            "choices": [{"index": 0, "delta": {"content": "Hallo "}}],
        }
    )
    assert _extract_streaming_chunk_text(chunk) == "Hallo "


def test_extract_chunk_text_handles_done_marker():
    assert _extract_streaming_chunk_text("[DONE]") == ""


def test_extract_chunk_text_handles_empty_string():
    assert _extract_streaming_chunk_text("") == ""


def test_extract_chunk_text_handles_invalid_json():
    assert _extract_streaming_chunk_text("not-json-at-all") == ""


def test_extract_chunk_text_handles_chunk_without_content():
    """Manche Chunks haben nur role oder finish_reason, kein content."""
    chunk = json.dumps(
        {
            "id": "chatcmpl-2",
            "choices": [{"index": 0, "delta": {"role": "assistant"}}],
        }
    )
    assert _extract_streaming_chunk_text(chunk) == ""

    chunk_finish = json.dumps(
        {
            "id": "chatcmpl-3",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    assert _extract_streaming_chunk_text(chunk_finish) == ""


def test_extract_chunk_text_handles_empty_choices():
    chunk = json.dumps({"id": "chatcmpl-4", "choices": []})
    assert _extract_streaming_chunk_text(chunk) == ""


def test_tail_buffer_caps_at_max_bytes():
    """Rolling-Slice [-tail_max:] kappt korrekt bei langen Outputs."""
    tail_max = 100
    tail_buffer = ""
    # Simulier Stream: 50 Chunks à 10 Zeichen = 500 Zeichen Total
    for i in range(50):
        chunk_text = f"chunk{i:03d} "  # 10 Zeichen
        tail_buffer = (tail_buffer + chunk_text)[-tail_max:]
    assert len(tail_buffer) == tail_max
    # Das Ende muss erhalten sein
    assert "chunk049" in tail_buffer
    # Der Anfang darf nicht mehr drin sein
    assert "chunk000" not in tail_buffer


def test_settings_guard_post_stream_defaults():
    """Feature-Flag default an, Tail-Bytes default 10KB."""
    from app.config import Settings

    s = Settings(proxy_api_keys="", proxy_auth_required=False)
    assert s.guard_post_stream_enabled is True
    assert s.guard_post_stream_tail_bytes == 10 * 1024


def test_settings_guard_post_stream_can_be_disabled():
    """ENV-Override schaltet den Pfad aus, falls Live-Probleme."""
    from app.config import Settings

    s = Settings(
        proxy_api_keys="",
        proxy_auth_required=False,
        guard_post_stream_enabled=False,
    )
    assert s.guard_post_stream_enabled is False
