"""tests/test_guard_hooks.py — Unit + integration tests for F6 guard hooks.

Coverage:
  - PIIRedactHook: email, phone, IBAN, CC masking + count tag; no false positives.
  - AIActTagHook: ai-generated tag set; text unchanged.
  - load_hooks: built-in names, unknown name (ignored), empty spec.
  - run_pre_hooks: tag merging, blocking, fail-open on exception.
  - run_post_hooks: tag merging, fail-open on exception.
  - Endpoint integration: pii_redact active → provider receives redacted text;
    response carries X-Orkoprox-Hook-pii-redacted header.
"""

from __future__ import annotations

import importlib
import os
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.hooks import (
    AIActTagHook,
    GuardHook,
    HookResult,
    PIIRedactHook,
    load_hooks,
    run_post_hooks,
    run_pre_hooks,
)


# ── PIIRedactHook ─────────────────────────────────────────────────────────────

class TestPIIRedactHookPre:
    def setup_method(self):
        self.hook = PIIRedactHook()

    def test_email_masked(self):
        result = self.hook.pre("Contact me at user@example.com please.", {})
        assert "[EMAIL]" in result.text
        assert "user@example.com" not in result.text
        assert int(result.tags["pii-redacted"]) >= 1

    def test_iban_masked(self):
        result = self.hook.pre("My IBAN is DE89370400440532013000.", {})
        assert "[IBAN]" in result.text
        assert "DE89370400440532013000" not in result.text
        assert int(result.tags["pii-redacted"]) >= 1

    def test_credit_card_masked(self):
        result = self.hook.pre("Card number 4111 1111 1111 1111 please charge it.", {})
        assert "[CARD]" in result.text
        assert int(result.tags["pii-redacted"]) >= 1

    def test_phone_de_masked(self):
        result = self.hook.pre("Call me at +49 30 12345678 tomorrow.", {})
        assert "[PHONE]" in result.text
        assert int(result.tags["pii-redacted"]) >= 1

    def test_multiple_pii_count(self):
        text = "Email user@test.com and also admin@test.com"
        result = self.hook.pre(text, {})
        assert int(result.tags["pii-redacted"]) == 2

    def test_no_pii_count_zero(self):
        result = self.hook.pre("The weather is nice today.", {})
        assert result.text == "The weather is nice today."
        assert result.tags["pii-redacted"] == "0"

    def test_no_false_positive_normal_text(self):
        text = "Version 2.0 released. See changelog for details."
        result = self.hook.pre(text, {})
        assert result.text == text
        assert result.tags["pii-redacted"] == "0"

    def test_not_blocked(self):
        result = self.hook.pre("user@example.com", {})
        assert result.blocked is False

    def test_post_noop(self):
        result = self.hook.post("some text", {})
        assert result.text == "some text"
        assert result.tags == {}
        assert result.blocked is False


# ── AIActTagHook ──────────────────────────────────────────────────────────────

class TestAIActTagHook:
    def setup_method(self):
        self.hook = AIActTagHook()

    def test_post_sets_ai_generated_tag(self):
        result = self.hook.post("Here is your answer.", {})
        assert result.tags.get("ai-generated") == "true"

    def test_post_text_unchanged(self):
        text = "Here is your answer."
        result = self.hook.post(text, {})
        assert result.text == text

    def test_post_not_blocked(self):
        result = self.hook.post("text", {})
        assert result.blocked is False

    def test_pre_noop(self):
        result = self.hook.pre("user input", {})
        assert result.text == "user input"
        assert result.tags == {}
        assert result.blocked is False


# ── load_hooks ────────────────────────────────────────────────────────────────

class TestLoadHooks:
    def test_builtin_pii_redact(self):
        hooks = load_hooks("pii_redact")
        assert len(hooks) == 1
        assert hooks[0].name == "pii_redact"

    def test_builtin_ai_act_tag(self):
        hooks = load_hooks("ai_act_tag")
        assert len(hooks) == 1
        assert hooks[0].name == "ai_act_tag"

    def test_both_builtins(self):
        hooks = load_hooks("pii_redact,ai_act_tag")
        assert len(hooks) == 2
        names = [h.name for h in hooks]
        assert "pii_redact" in names
        assert "ai_act_tag" in names

    def test_unknown_name_ignored(self):
        hooks = load_hooks("pii_redact,nonexistent_hook")
        assert len(hooks) == 1
        assert hooks[0].name == "pii_redact"

    def test_empty_spec_returns_empty(self):
        assert load_hooks("") == []

    def test_whitespace_only_returns_empty(self):
        assert load_hooks("   ") == []

    def test_unknown_only_returns_empty_no_crash(self):
        hooks = load_hooks("totally_unknown")
        assert hooks == []

    def test_dotted_path_unknown_module_ignored(self):
        # a dotted path to a non-existent module must not crash
        hooks = load_hooks("no.such.module:MyHook")
        assert hooks == []


# ── run_pre_hooks ─────────────────────────────────────────────────────────────

class TestRunPreHooks:
    def test_tags_merged(self):
        hooks = load_hooks("pii_redact,ai_act_tag")
        text, tags, blocked, reason = run_pre_hooks(hooks, "Hello world", {})
        # pii_redact always emits pii-redacted; ai_act_tag pre is noop (no tag)
        assert "pii-redacted" in tags
        assert not blocked

    def test_text_transformed_pii(self):
        hooks = load_hooks("pii_redact")
        text, tags, blocked, _ = run_pre_hooks(hooks, "my email is a@b.com", {})
        assert "[EMAIL]" in text
        assert "a@b.com" not in text

    def test_blocking_hook_stops_chain(self):
        class BlockingHook:
            name = "blocker"

            def pre(self, text: str, context: dict) -> HookResult:
                return HookResult(text=text, blocked=True, block_reason="test block")

            def post(self, text: str, context: dict) -> HookResult:
                return HookResult(text=text)

        class NeverCalledHook:
            name = "never"
            called = False

            def pre(self, text: str, context: dict) -> HookResult:
                NeverCalledHook.called = True
                return HookResult(text=text)

            def post(self, text: str, context: dict) -> HookResult:
                return HookResult(text=text)

        hooks: list[GuardHook] = [BlockingHook(), NeverCalledHook()]  # type: ignore[list-item]
        text, tags, blocked, reason = run_pre_hooks(hooks, "input", {})
        assert blocked is True
        assert reason == "test block"
        assert NeverCalledHook.called is False

    def test_raising_hook_fail_open(self):
        class RaisingHook:
            name = "raiser"

            def pre(self, text: str, context: dict) -> HookResult:
                raise RuntimeError("deliberate error")

            def post(self, text: str, context: dict) -> HookResult:
                return HookResult(text=text)

        hooks: list[GuardHook] = [RaisingHook(), PIIRedactHook()]  # type: ignore[list-item]
        # Should NOT raise; PIIRedactHook should still run
        text, tags, blocked, _ = run_pre_hooks(hooks, "user@example.com", {})
        assert not blocked
        assert "[EMAIL]" in text  # PIIRedactHook ran after the failing one
        assert "pii-redacted" in tags


# ── run_post_hooks ────────────────────────────────────────────────────────────

class TestRunPostHooks:
    def test_ai_act_tag_set(self):
        hooks = load_hooks("ai_act_tag")
        text, tags = run_post_hooks(hooks, "response text", {})
        assert tags.get("ai-generated") == "true"
        assert text == "response text"

    def test_raising_post_hook_fail_open(self):
        class RaisingHook:
            name = "raiser"

            def pre(self, text: str, context: dict) -> HookResult:
                return HookResult(text=text)

            def post(self, text: str, context: dict) -> HookResult:
                raise RuntimeError("deliberate post error")

        hooks: list[GuardHook] = [RaisingHook(), AIActTagHook()]  # type: ignore[list-item]
        text, tags = run_post_hooks(hooks, "output", {})
        # AIActTagHook should still have run
        assert tags.get("ai-generated") == "true"
        assert text == "output"

    def test_tags_merged_from_multiple_hooks(self):
        class TagHook:
            name = "tagger"

            def pre(self, text: str, context: dict) -> HookResult:
                return HookResult(text=text)

            def post(self, text: str, context: dict) -> HookResult:
                return HookResult(text=text, tags={"custom-tag": "yes"})

        hooks: list[GuardHook] = [TagHook(), AIActTagHook()]  # type: ignore[list-item]
        _, tags = run_post_hooks(hooks, "output", {})
        assert tags.get("custom-tag") == "yes"
        assert tags.get("ai-generated") == "true"


# ── Endpoint integration ──────────────────────────────────────────────────────

_FAKE_OPENAI_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-test-hooks",
    "object": "chat.completion",
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Redacted reply."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


@pytest.fixture()
def client_with_hooks(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with GUARD_HOOKS=pii_redact active and a fake provider."""
    monkeypatch.setenv("PRIMARY_PROVIDER", "mock")
    monkeypatch.setenv("DEFAULT_PROVIDER", "mock")
    monkeypatch.setenv("FALLBACK_PROVIDERS", "mock")
    monkeypatch.setenv("PROXY_API_KEYS", "test-key")
    monkeypatch.setenv("GUARD_HOOKS", "pii_redact")
    monkeypatch.delenv("REDIS_URL", raising=False)

    import app.config
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.main)

    from tests.mock_provider import TestMockProvider
    from app.providers.router import PROVIDER_ALIASES

    PROVIDER_ALIASES["mock"] = "mock"
    app.main.provider_registry._providers["mock"] = TestMockProvider()

    with TestClient(app.main.app) as test_client:
        yield test_client

    for key in [
        "PRIMARY_PROVIDER", "DEFAULT_PROVIDER", "FALLBACK_PROVIDERS",
        "PROXY_API_KEYS", "GUARD_HOOKS", "REDIS_URL",
    ]:
        os.environ.pop(key, None)


class TestGuardHooksEndpoint:
    def test_pii_redacted_in_provider_payload(self, client_with_hooks: TestClient):
        """The provider must receive [EMAIL] instead of the raw address."""
        import app.main as main_mod

        captured: list[dict] = []

        async def _capturing_chat_completions(
            raw_model: str, payload: dict[str, Any], ctx: Any
        ):
            captured.append(payload)
            return ("mock", "test-model", dict(_FAKE_OPENAI_RESPONSE), {})

        with patch.object(main_mod.provider_registry, "chat_completions", _capturing_chat_completions):
            resp = client_with_hooks.post(
                "/v1/chat/completions",
                headers={"x-api-key": "test-key"},
                json={
                    "model": "mock-model",
                    "messages": [{"role": "user", "content": "My email is secret@example.com"}],
                },
            )

        assert resp.status_code == 200
        assert len(captured) == 1
        # Find the last user message in the captured payload
        user_msgs = [m for m in captured[0]["messages"] if m.get("role") == "user"]
        assert user_msgs, "no user message in captured payload"
        content = user_msgs[-1]["content"]
        assert "[EMAIL]" in content, f"expected [EMAIL] in payload content, got: {content!r}"
        assert "secret@example.com" not in content

    def test_pii_redacted_header_set(self, client_with_hooks: TestClient):
        """Response must carry X-Orkoprox-Hook-pii-redacted header."""
        import app.main as main_mod

        async def _fake(raw_model: str, payload: dict[str, Any], ctx: Any):
            return ("mock", "test-model", dict(_FAKE_OPENAI_RESPONSE), {})

        with patch.object(main_mod.provider_registry, "chat_completions", _fake):
            resp = client_with_hooks.post(
                "/v1/chat/completions",
                headers={"x-api-key": "test-key"},
                json={
                    "model": "mock-model",
                    "messages": [{"role": "user", "content": "Email me at a@b.com"}],
                },
            )

        assert resp.status_code == 200
        header_val = resp.headers.get("x-orkoprox-hook-pii-redacted")
        assert header_val is not None, "X-Orkoprox-Hook-pii-redacted header missing"
        assert int(header_val) >= 1

    def test_no_pii_header_still_present_with_count_zero(self, client_with_hooks: TestClient):
        """Even when no PII found the header is present with count 0."""
        import app.main as main_mod

        async def _fake(raw_model: str, payload: dict[str, Any], ctx: Any):
            return ("mock", "test-model", dict(_FAKE_OPENAI_RESPONSE), {})

        with patch.object(main_mod.provider_registry, "chat_completions", _fake):
            resp = client_with_hooks.post(
                "/v1/chat/completions",
                headers={"x-api-key": "test-key"},
                json={
                    "model": "mock-model",
                    "messages": [{"role": "user", "content": "Hello there"}],
                },
            )

        assert resp.status_code == 200
        header_val = resp.headers.get("x-orkoprox-hook-pii-redacted")
        assert header_val == "0"
