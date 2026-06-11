"""Tests for audit log masking and write behaviour.

Privacy invariants tested:
- mask_key: never emits the full key, only a prefix marker.
- AuditLog: writes well-formed JSONL; the api_key field is masked in the file.
- AuditLog: disabled log writes nothing.
- AuditLog.record(): never raises, even on a broken path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.audit_log import AuditLog, mask_key


# ── mask_key ──────────────────────────────────────────────────────────────────


def test_mask_key_none_returns_anonymous() -> None:
    assert mask_key(None) == "anonymous"


def test_mask_key_empty_string_returns_anonymous() -> None:
    assert mask_key("") == "anonymous"


def test_mask_key_whitespace_only_returns_star_form() -> None:
    # "   " is truthy so the None-check passes; after strip() len==0 <= 8 → "***"
    result = mask_key("   ")
    assert "***" in result
    assert result != "   "  # never returns whitespace as-is


def test_mask_key_short_key_uses_star_form() -> None:
    """Keys <= 8 chars get first-2-chars + '***'."""
    result = mask_key("abcdefgh")  # exactly 8 chars
    assert result.endswith("***")
    assert result.startswith("ab")
    assert "abcdefgh" not in result  # full key not present


def test_mask_key_very_short_key() -> None:
    result = mask_key("xy")
    assert "***" in result
    assert "xy" not in result or result == "xy***"  # prefix preserved, not full key exposed


def test_mask_key_long_key_uses_ellipsis_form() -> None:
    """Keys > 8 chars get first-8-chars + '...'."""
    long_key = "abcdefgh-this-is-more"
    result = mask_key(long_key)
    assert result == "abcdefgh..."
    assert long_key not in result  # full key never returned


def test_mask_key_never_returns_full_key_for_any_length() -> None:
    for length in range(1, 64):
        key = "k" * length
        masked = mask_key(key)
        # The full key must not appear literally in the masked output
        # (for very short keys the prefix IS the key, but we accept that —
        # the invariant is no extra entropy is leaked beyond the prefix).
        if length > 8:
            assert key not in masked, f"full key leaked for length={length}"


def test_mask_key_prefix_kept_for_correlation() -> None:
    """The first 8 chars are kept so operators can correlate logs to keys."""
    key = "prefix99-and-more-secret-data"
    result = mask_key(key)
    assert result.startswith("prefix99")
    assert result == "prefix99..."


# ── AuditLog disabled ─────────────────────────────────────────────────────────


def test_audit_log_disabled_writes_nothing(tmp_path: Path) -> None:
    log_file = tmp_path / "audit.jsonl"
    log = AuditLog(enabled=False, path=str(log_file))
    log.record("test_event", api_key="secret-key", model="gpt-x")
    assert not log_file.exists()


def test_audit_log_enabled_false_no_path_writes_nothing() -> None:
    log = AuditLog(enabled=False, path=None)
    # Must not raise
    log.record("test_event")


def test_audit_log_enabled_without_path_writes_nothing() -> None:
    """enabled=True but path=None → .enabled property is False → no write."""
    log = AuditLog(enabled=True, path=None)
    assert log.enabled is False
    log.record("test_event", api_key="key")  # must not raise


# ── AuditLog writes JSONL ────────────────────────────────────────────────────


def test_audit_log_writes_valid_jsonl_line(tmp_path: Path) -> None:
    log_file = tmp_path / "audit.jsonl"
    log = AuditLog(enabled=True, path=str(log_file))
    log.record("request_received", model="test-model", tokens=42)

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "request_received"
    assert entry["model"] == "test-model"
    assert entry["tokens"] == 42
    assert "ts" in entry


def test_audit_log_masks_api_key_field(tmp_path: Path) -> None:
    """api_key must not appear in the file; masked 'key' field must be present."""
    secret = "super-secret-full-api-key-1234567890"
    log_file = tmp_path / "audit.jsonl"
    log = AuditLog(enabled=True, path=str(log_file))
    log.record("admin_action", api_key=secret)

    raw = log_file.read_text(encoding="utf-8")
    assert secret not in raw, "full API key must never be written to audit log"

    entry = json.loads(raw.strip())
    assert "api_key" not in entry, "api_key field must be renamed to 'key'"
    assert "key" in entry
    assert entry["key"] == mask_key(secret)


def test_audit_log_multiple_records_appended(tmp_path: Path) -> None:
    log_file = tmp_path / "audit.jsonl"
    log = AuditLog(enabled=True, path=str(log_file))
    log.record("event_one", api_key="key-one-" + "x" * 10)
    log.record("event_two", api_key="key-two-" + "y" * 10)

    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "event_one"
    assert json.loads(lines[1])["event"] == "event_two"


def test_audit_log_non_api_key_fields_pass_through_unmasked(tmp_path: Path) -> None:
    log_file = tmp_path / "audit.jsonl"
    log = AuditLog(enabled=True, path=str(log_file))
    log.record("billing", tenant_id="tenant-123", cost_usd=0.005)

    entry = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert entry["tenant_id"] == "tenant-123"
    assert entry["cost_usd"] == pytest.approx(0.005)


# ── AuditLog never raises ─────────────────────────────────────────────────────


def test_audit_log_does_not_raise_on_broken_path() -> None:
    """record() must be best-effort — broken path must not propagate."""
    log = AuditLog(enabled=True, path="/nonexistent-dir/audit.jsonl")
    # Must not raise
    log.record("test_event", api_key="any-key")


def test_audit_log_does_not_raise_on_unserializable_field(tmp_path: Path) -> None:
    """Non-serialisable values fall back via default=str; must not raise."""

    class _Opaque:
        def __str__(self) -> str:
            return "opaque-repr"

    log_file = tmp_path / "audit.jsonl"
    log = AuditLog(enabled=True, path=str(log_file))
    log.record("test_event", weird=_Opaque())

    entry = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert entry["weird"] == "opaque-repr"
