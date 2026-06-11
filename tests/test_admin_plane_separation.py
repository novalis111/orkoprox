"""Security tests for admin-plane / data-plane separation.

The critical invariant: a data-plane key MUST NEVER authenticate on the admin
plane — a leaked client key cannot mint or revoke keys. Tested here as pure
unit calls against enforce_admin_auth so there are no module-reload sideffects.
"""

from __future__ import annotations

import pytest

from app.admin_auth import enforce_admin_auth
from app.config import Settings
from app.errors import ProxyError

# ── fixtures ────────────────────────────────────────────────────────────────

DATA_KEY = "test-data-key-" + "x" * 26  # 40 chars, valid data-plane key
ADMIN_KEY = "test-admin-key-" + "a" * 25  # 40 chars, valid admin key


def _settings(*, admin_api_keys: str = "", proxy_api_keys: str = DATA_KEY) -> Settings:
    """Build a Settings with auth required and no environment-level production checks."""
    return Settings(
        proxy_auth_required=False,
        proxy_api_keys=proxy_api_keys,
        admin_api_keys=admin_api_keys,
    )


# ── disabled plane ───────────────────────────────────────────────────────────


def test_admin_plane_disabled_when_no_admin_keys_configured() -> None:
    """Empty ADMIN_API_KEYS → 403 admin_plane_disabled, not 401."""
    settings = _settings(admin_api_keys="")
    with pytest.raises(ProxyError) as exc_info:
        enforce_admin_auth(settings, x_api_key="anything", authorization=None)
    err = exc_info.value
    assert err.http_status == 403
    assert err.code == "admin_plane_disabled"


def test_admin_plane_disabled_even_with_valid_data_key() -> None:
    """Admin plane disabled even if the presented key is a valid data-plane key."""
    settings = _settings(admin_api_keys="")
    with pytest.raises(ProxyError) as exc_info:
        enforce_admin_auth(settings, x_api_key=DATA_KEY, authorization=None)
    err = exc_info.value
    assert err.http_status == 403
    assert err.code == "admin_plane_disabled"


# ── CVE-class: data-plane key rejected on admin plane ────────────────────────


def test_data_plane_key_rejected_on_admin_plane_xapikey() -> None:
    """THE core security invariant: data-plane key → 403, not 200 or 401.

    A leaked client key MUST NOT gain admin access even if it is a valid key
    on the data plane.
    """
    settings = _settings(
        proxy_api_keys=DATA_KEY,
        admin_api_keys=ADMIN_KEY,
    )
    with pytest.raises(ProxyError) as exc_info:
        enforce_admin_auth(settings, x_api_key=DATA_KEY, authorization=None)
    err = exc_info.value
    assert err.http_status == 403
    assert err.code == "admin_forbidden"


def test_data_plane_key_rejected_on_admin_plane_bearer() -> None:
    """Same invariant via Authorization Bearer header."""
    settings = _settings(
        proxy_api_keys=DATA_KEY,
        admin_api_keys=ADMIN_KEY,
    )
    with pytest.raises(ProxyError) as exc_info:
        enforce_admin_auth(
            settings, x_api_key=None, authorization=f"Bearer {DATA_KEY}"
        )
    err = exc_info.value
    assert err.http_status == 403
    assert err.code == "admin_forbidden"


# ── missing key ──────────────────────────────────────────────────────────────


def test_no_key_presented_raises_401() -> None:
    settings = _settings(admin_api_keys=ADMIN_KEY)
    with pytest.raises(ProxyError) as exc_info:
        enforce_admin_auth(settings, x_api_key=None, authorization=None)
    err = exc_info.value
    assert err.http_status == 401
    assert err.code == "missing_admin_key"


def test_empty_xapikey_treated_as_missing() -> None:
    settings = _settings(admin_api_keys=ADMIN_KEY)
    with pytest.raises(ProxyError) as exc_info:
        enforce_admin_auth(settings, x_api_key="   ", authorization=None)
    err = exc_info.value
    assert err.http_status == 401
    assert err.code == "missing_admin_key"


def test_malformed_authorization_header_treated_as_missing() -> None:
    """Non-Bearer scheme → key extraction fails → 401."""
    settings = _settings(admin_api_keys=ADMIN_KEY)
    with pytest.raises(ProxyError) as exc_info:
        enforce_admin_auth(
            settings, x_api_key=None, authorization="Basic dXNlcjpwYXNz"
        )
    err = exc_info.value
    assert err.http_status == 401
    assert err.code == "missing_admin_key"


# ── garbage key ──────────────────────────────────────────────────────────────


def test_garbage_key_raises_403() -> None:
    """An unrecognised key gets 403 (not 401) — no hint about key validity."""
    settings = _settings(admin_api_keys=ADMIN_KEY)
    with pytest.raises(ProxyError) as exc_info:
        enforce_admin_auth(
            settings, x_api_key="completely-wrong-key", authorization=None
        )
    err = exc_info.value
    assert err.http_status == 403
    assert err.code == "admin_forbidden"


# ── valid admin key accepted ──────────────────────────────────────────────────


def test_valid_admin_key_via_xapikey_returns_key() -> None:
    settings = _settings(admin_api_keys=ADMIN_KEY)
    result = enforce_admin_auth(settings, x_api_key=ADMIN_KEY, authorization=None)
    assert result == ADMIN_KEY


def test_valid_admin_key_via_bearer_returns_key() -> None:
    settings = _settings(admin_api_keys=ADMIN_KEY)
    result = enforce_admin_auth(
        settings, x_api_key=None, authorization=f"Bearer {ADMIN_KEY}"
    )
    assert result == ADMIN_KEY


def test_xapikey_takes_precedence_over_authorization() -> None:
    """X-API-Key header wins when both headers present."""
    settings = _settings(admin_api_keys=ADMIN_KEY)
    # X-API-Key = valid, Authorization = garbage — should succeed
    result = enforce_admin_auth(
        settings, x_api_key=ADMIN_KEY, authorization="Bearer garbage"
    )
    assert result == ADMIN_KEY


def test_multiple_admin_keys_any_is_valid() -> None:
    """Comma-separated admin keys — any one of them should authenticate."""
    second_admin = "second-admin-key-" + "b" * 23  # 40 chars
    settings = _settings(admin_api_keys=f"{ADMIN_KEY},{second_admin}")
    assert enforce_admin_auth(settings, x_api_key=ADMIN_KEY, authorization=None) == ADMIN_KEY
    assert (
        enforce_admin_auth(settings, x_api_key=second_admin, authorization=None)
        == second_admin
    )
