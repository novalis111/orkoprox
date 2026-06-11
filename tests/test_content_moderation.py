"""Tests for app.services.content_moderation (Qwen3Guard integration)."""
from __future__ import annotations


import pytest

from app.services.content_moderation import (
    POST_BLOCK_CATEGORIES,
    PRE_BLOCK_CATEGORIES,
    SAFE_FALLBACK_TEXT,
    GuardDecision,
    _is_blocked,
    _parse_guard_output,
)


class TestParser:
    def test_parses_safe_none(self):
        safety, cats = _parse_guard_output("Safety: Safe\nCategories: None")
        assert safety == "Safe"
        assert cats == ()

    def test_parses_unsafe_violent(self):
        safety, cats = _parse_guard_output("Safety: Unsafe\nCategories: Violent")
        assert safety == "Unsafe"
        assert cats == ("Violent",)

    def test_parses_multiple_categories(self):
        safety, cats = _parse_guard_output("Safety: Unsafe\nCategories: Violent, PII")
        assert safety == "Unsafe"
        assert "Violent" in cats
        assert "PII" in cats

    def test_parses_controversial(self):
        safety, cats = _parse_guard_output("Safety: Controversial\nCategories: Jailbreak")
        assert safety == "Controversial"
        assert cats == ("Jailbreak",)

    def test_parses_lowercase_n_a(self):
        safety, cats = _parse_guard_output("Safety: Safe\nCategories: n/a")
        assert cats == ()

    def test_parses_empty_categories(self):
        safety, cats = _parse_guard_output("Safety: Safe\nCategories: ")
        assert cats == ()

    def test_parses_unknown_format(self):
        safety, cats = _parse_guard_output("random text without safety marker")
        assert safety == "Unknown"
        assert cats == ()

    def test_handles_ovh_variant_capitalization(self):
        """OVH liefert 'Non-violent Illegal Acts' (kleines v) — eine
        Schreibvariante der offiziellen Kategorie."""
        safety, cats = _parse_guard_output(
            "Safety: Unsafe\nCategories: Non-violent Illegal Acts"
        )
        assert safety == "Unsafe"
        assert cats == ("Non-violent Illegal Acts",)


class TestBlockDecision:
    def test_safe_never_blocked(self):
        assert not _is_blocked("Safe", ("Violent",), PRE_BLOCK_CATEGORIES)

    def test_controversial_never_blocked(self):
        assert not _is_blocked("Controversial", ("Jailbreak",), PRE_BLOCK_CATEGORIES)

    def test_unsafe_violent_blocked_pre(self):
        assert _is_blocked("Unsafe", ("Violent",), PRE_BLOCK_CATEGORIES)

    def test_unsafe_jailbreak_blocked_pre(self):
        assert _is_blocked("Unsafe", ("Jailbreak",), PRE_BLOCK_CATEGORIES)

    def test_unsafe_pii_blocked_pre(self):
        assert _is_blocked("Unsafe", ("PII",), PRE_BLOCK_CATEGORIES)

    def test_unsafe_self_harm_blocked_pre(self):
        assert _is_blocked("Unsafe", ("Suicide & Self-Harm",), PRE_BLOCK_CATEGORIES)

    def test_unsafe_unethical_not_blocked_pre(self):
        """Unethical Acts blockiert NUR Post-Filter, nicht Pre."""
        assert not _is_blocked("Unsafe", ("Unethical Acts",), PRE_BLOCK_CATEGORIES)

    def test_unsafe_unethical_blocked_post(self):
        assert _is_blocked("Unsafe", ("Unethical Acts",), POST_BLOCK_CATEGORIES)

    def test_unsafe_jailbreak_not_blocked_post(self):
        """Jailbreak ist input-only, Post-Filter ignoriert."""
        assert not _is_blocked("Unsafe", ("Jailbreak",), POST_BLOCK_CATEGORIES)

    def test_unsafe_ovh_variant_blocked_pre(self):
        """OVH-Schreibvariante 'Non-violent Illegal Acts' wird auch geblockt."""
        assert _is_blocked("Unsafe", ("Non-violent Illegal Acts",), PRE_BLOCK_CATEGORIES)


class TestGuardDecision:
    def test_to_audit_dict(self):
        d = GuardDecision(
            safety="Unsafe",
            categories=("Violent", "PII"),
            is_blocked=True,
            reason="Unsafe/Violent,PII",
            latency_ms=123.4,
            model="Qwen3Guard-Gen-0.6B",
        )
        out = d.to_audit_dict()
        assert out["safety"] == "Unsafe"
        assert out["categories"] == ["Violent", "PII"]
        assert out["blocked"] is True
        assert out["latency_ms"] == 123  # int-truncated
        assert out["model"] == "Qwen3Guard-Gen-0.6B"


class TestSafeFallback:
    def test_safe_fallback_is_german(self):
        """SAFE_FALLBACK_TEXT must be in German."""
        assert "Entschuldigung" in SAFE_FALLBACK_TEXT
        assert "anders" in SAFE_FALLBACK_TEXT


# ─── Async-Tests fuer check_safety_pre/post (mit fake-base_url) ──────────


@pytest.mark.asyncio
async def test_check_safety_pre_fail_open_on_invalid_url():
    """Bei unbekannter URL + fail_open → Decision mit guard_unavailable."""
    from app.services.content_moderation import check_safety_pre

    decision = await check_safety_pre(
        "test input",
        base_url="http://nonexistent.invalid",
        api_key="fake",
        timeout_s=0.5,
        fail_open=True,
    )
    assert decision.safety == "Unknown"
    assert decision.is_blocked is False
    assert decision.reason == "guard_unavailable"


@pytest.mark.asyncio
async def test_check_safety_pre_fail_closed_on_invalid_url():
    """Bei fail_open=False → blockt (defensive)."""
    from app.services.content_moderation import check_safety_pre

    decision = await check_safety_pre(
        "test input",
        base_url="http://nonexistent.invalid",
        api_key="fake",
        timeout_s=0.5,
        fail_open=False,
    )
    assert decision.is_blocked is True


@pytest.mark.asyncio
async def test_check_safety_post_fail_open():
    from app.services.content_moderation import check_safety_post

    decision = await check_safety_post(
        "test input",
        "test output",
        base_url="http://nonexistent.invalid",
        api_key="fake",
        timeout_s=0.5,
        fail_open=True,
    )
    assert decision.safety == "Unknown"
    assert decision.is_blocked is False
