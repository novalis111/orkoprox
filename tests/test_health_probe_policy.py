"""Tests for the X-Health-Probe policy marker in _apply_model_policy.

_apply_model_policy is now essentially a pass-through but retains the
health-probe marker for telemetry.
"""

from __future__ import annotations


def _make_registry():
    from app.config import Settings
    from app.providers.router import ProviderRegistry

    settings = Settings(proxy_api_keys="")
    return ProviderRegistry(settings), settings


def test_policy_ohne_health_probe_passes_through():
    """Ohne X-Health-Probe wird payload unverändert durchgereicht."""
    from app.providers.base import ProviderRequestContext

    registry, _ = _make_registry()
    ctx = ProviderRequestContext(request_id="r1", forward_headers={}, is_health_probe=False)
    payload = {"model": "low", "messages": [{"role": "user", "content": "x"}], "max_tokens": 5}

    out, policy = registry._apply_model_policy("low", payload, ctx=ctx)

    assert out["max_tokens"] == 5
    assert policy == {}


def test_policy_mit_health_probe_emits_marker():
    """Mit X-Health-Probe bekommen wir den Marker für Telemetrie."""
    from app.providers.base import ProviderRequestContext

    registry, _ = _make_registry()
    ctx = ProviderRequestContext(request_id="r1", forward_headers={}, is_health_probe=True)
    payload = {"model": "low", "messages": [{"role": "user", "content": "x"}], "max_tokens": 5}

    out, policy = registry._apply_model_policy("low", payload, ctx=ctx)

    assert out["max_tokens"] == 5
    assert policy == {"health_probe_bypass": True}


def test_policy_passthrough_for_high_max_tokens():
    """Auch große max_tokens werden nicht gecappt — OVH cappt selbst auf Modell-Limit."""
    from app.providers.base import ProviderRequestContext

    registry, _ = _make_registry()
    ctx = ProviderRequestContext(request_id="r1", forward_headers={}, is_health_probe=False)
    payload = {
        "model": "Mistral-Small-3.2-24B-Instruct-2506",
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 100000,
    }

    out, _ = registry._apply_model_policy(
        "Mistral-Small-3.2-24B-Instruct-2506", payload, ctx=ctx
    )

    assert out["max_tokens"] == 100000


def test_request_context_parst_x_health_probe_header():
    """Die _make_request_context-Funktion setzt is_health_probe aus dem Header."""
    from starlette.datastructures import Headers

    class FakeRequest:
        def __init__(self, headers: dict[str, str]):
            self.headers = Headers(headers)

            class State:
                request_id = "req-test"

            self.state = State()

    from app import main

    ctx_yes = main._make_request_context(FakeRequest({"x-health-probe": "1"}))
    assert ctx_yes.is_health_probe is True

    ctx_no = main._make_request_context(FakeRequest({}))
    assert ctx_no.is_health_probe is False

    ctx_other = main._make_request_context(FakeRequest({"x-health-probe": "0"}))
    assert ctx_other.is_health_probe is False
