"""Guard use-case bypass tests.

`_should_skip_guard` has two bypass modes:
1. X-Skip-Content-Guard:true header + auth key in guard_bypass_keys
2. X-Use-Case header + use-case in guard_pre_skip_use_cases (server-side)

Mode 2 lets benign business classifiers skip the pre-guard server-side, so a
legitimate request is not misclassified as a policy violation. The post-guard
still runs, so model output stays screened.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.main import _should_skip_guard


def _fake_request(headers: dict[str, str]) -> SimpleNamespace:
    """Minimaler Request-Stub mit headers-dict."""

    class _Headers:
        def __init__(self, h: dict[str, str]) -> None:
            self._h = {k.lower(): v for k, v in h.items()}

        def get(self, key: str, default: str = "") -> str:
            return self._h.get(key.lower(), default)

    return SimpleNamespace(headers=_Headers(headers))


def test_guard_disabled_globally_skips():
    """Wenn guard_enabled=False global, immer skip."""
    from app.main import settings

    original = settings.guard_enabled
    try:
        settings.guard_enabled = False
        assert _should_skip_guard("any_key", _fake_request({})) is True
    finally:
        settings.guard_enabled = original


def test_use_case_mode_classify_bypasses_pre_guard():
    """F4: X-Use-Case=mode_classify -> Pre-Guard-Skip ohne Auth-Key-Whitelist."""
    req = _fake_request({"X-Use-Case": "mode_classify"})
    assert _should_skip_guard("any_random_key", req) is True


def test_use_case_intent_classify_bypasses():
    """F4: X-Use-Case=intent_classify ist auch in der Default-Whitelist."""
    req = _fake_request({"X-Use-Case": "intent_classify"})
    assert _should_skip_guard("any_key", req) is True


def test_use_case_unknown_does_not_bypass():
    """F4: nicht-whitelisted Use-Case skipt NICHT (verhindert Self-Whitelisting)."""
    req = _fake_request({"X-Use-Case": "unknown_use_case_xyz"})
    assert _should_skip_guard("any_key", req) is False


def test_use_case_chat_does_not_bypass():
    """F4: Default-Use-Case 'chat' ist nicht in Whitelist."""
    req = _fake_request({"X-Use-Case": "chat"})
    assert _should_skip_guard("any_key", req) is False


def test_no_headers_does_not_bypass():
    """Ohne X-Use-Case und ohne X-Skip-Content-Guard kein Bypass."""
    req = _fake_request({})
    assert _should_skip_guard("any_key", req) is False


def test_skip_content_guard_header_needs_whitelisted_key():
    """X-Skip-Content-Guard:true ohne Whitelist-Key ist kein Bypass."""
    req = _fake_request({"X-Skip-Content-Guard": "true"})
    # Modus 1 verlangt Auth-Key in guard_bypass_keys (default leer in test)
    assert _should_skip_guard("non_whitelisted_key", req) is False


def test_use_case_case_insensitive():
    """X-Use-Case ist case-insensitive (Header-Normalisierung)."""
    req = _fake_request({"X-Use-Case": "MODE_CLASSIFY"})
    assert _should_skip_guard("any_key", req) is True


def test_guard_pre_skip_use_case_set_default():
    """Default-Whitelist enthaelt mode_classify, intent_classify, plan_repair."""
    from app.main import settings

    expected = {"mode_classify", "intent_classify", "plan_repair"}
    assert expected.issubset(settings.guard_pre_skip_use_case_set)
