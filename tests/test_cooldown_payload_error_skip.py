from __future__ import annotations

import logging

import pytest

from app.config import Settings
from app.providers.base import ProviderError
from app.providers.router import ProviderRegistry


def _registry() -> ProviderRegistry:
    return ProviderRegistry(Settings(proxy_api_keys=""))


def _provider_error(status: int) -> ProviderError:
    """ProviderError wie ihn der OpenAI-Adapter baut (mit provider_status)."""
    return ProviderError(
        f"upstream error {status}",
        status_code=502,
        code="upstream_error",
        details={"provider_status": status, "provider_response": {"error": {"message": "boom"}}},
    )


@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_payload_error_does_not_trigger_cooldown(status: int) -> None:
    """F3: Ein request-spezifischer Validierungsfehler EINES Consumers darf
    den Provider NICHT fuer alle sperren."""
    registry = _registry()
    registry._start_cooldown("ovh", scope="chat", exc=_provider_error(status))
    assert registry._is_cooling_down("ovh", scope="chat") is False


def test_payload_error_skip_is_logged() -> None:
    """F3: Der Skip wird als eigenes Event geloggt (beobachtbar).

    Der App-Logger hat ``propagate = False`` (logging_utils.configure_logging),
    deshalb haengen wir einen eigenen Handler direkt an den Logger statt auf
    caplog-Propagation zum Root zu vertrauen.
    """
    registry = _registry()
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    app_logger = logging.getLogger("llm-unified-proxy")
    handler = _Capture()
    app_logger.addHandler(handler)
    prev_level = app_logger.level
    app_logger.setLevel(logging.INFO)
    try:
        registry._start_cooldown("ovh", scope="chat", exc=_provider_error(400))
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(prev_level)

    assert any("cooldown_skipped_payload_error" in rec.getMessage() for rec in records)


def test_rate_limit_429_still_triggers_cooldown() -> None:
    """F3-Negativfall: 429 hat den eigenen langen Cooldown-Pfad — bleibt aktiv."""
    registry = _registry()
    exc = ProviderError(
        "rate limited",
        status_code=429,
        code="upstream_error",
        details={"provider_status": 429},
    )
    registry._start_cooldown("ovh", scope="chat", exc=exc)
    assert registry._is_cooling_down("ovh", scope="chat") is True


def test_server_error_5xx_still_triggers_cooldown() -> None:
    """F3-Negativfall: echter Provider-Ausfall (5xx) muss Cooldown ausloesen."""
    registry = _registry()
    exc = ProviderError(
        "internal server error",
        status_code=502,
        code="upstream_error",
        details={"provider_status": 500},
    )
    registry._start_cooldown("ovh", scope="chat", exc=exc)
    assert registry._is_cooling_down("ovh", scope="chat") is True


def test_timeout_still_triggers_cooldown() -> None:
    """F3-Negativfall: Timeout (504, kein provider_status) -> Cooldown aktiv."""
    registry = _registry()
    exc = ProviderError(
        "ovh timeout",
        status_code=504,
        code="upstream_timeout",
        retryable=True,
        details={},
    )
    registry._start_cooldown("ovh", scope="chat", exc=exc)
    assert registry._is_cooling_down("ovh", scope="chat") is True


def test_connection_error_still_triggers_cooldown() -> None:
    """F3-Negativfall: ConnectionError (502 upstream_network_error) -> Cooldown."""
    registry = _registry()
    exc = ProviderError(
        "ovh network error",
        status_code=502,
        code="upstream_network_error",
        retryable=True,
        details={},
    )
    registry._start_cooldown("ovh", scope="chat", exc=exc)
    assert registry._is_cooling_down("ovh", scope="chat") is True


def test_auth_error_still_triggers_cooldown() -> None:
    """F3-Negativfall: Auth-Fehler (401/403) ist Provider-Konfigurationsproblem,
    kein Per-Request-Payload-Fehler -> Cooldown bleibt."""
    registry = _registry()
    for status in (401, 403):
        registry = _registry()
        exc = ProviderError(
            "unauthorized",
            status_code=502,
            code="upstream_error",
            details={"provider_status": status},
        )
        registry._start_cooldown("ovh", scope="chat", exc=exc)
        assert registry._is_cooling_down("ovh", scope="chat") is True


def test_stub_provider_never_cooled_down() -> None:
    """Bestehendes Verhalten unveraendert: Stub kriegt nie Cooldown."""
    registry = _registry()
    registry._start_cooldown("stub", scope="chat", exc=_provider_error(500))
    assert registry._is_cooling_down("stub", scope="chat") is False
