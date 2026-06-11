"""Tests for Settings config validation of admin-plane keys.

Covers:
- admin/proxy key overlap → ValueError
- production environment + short admin key → ValueError
- production environment + long admin key + no overlap → OK
- dev environment + short admin key → OK (no length check)
"""

from __future__ import annotations

import pytest

from app.config import Settings

# ── key constants ────────────────────────────────────────────────────────────

SHORT_KEY = "short-key"  # < 40 chars
LONG_PROXY_KEY = "proxy-data-plane-key-" + "p" * 19  # exactly 40 chars
LONG_ADMIN_KEY = "admin-plane-key-only-" + "a" * 19  # exactly 40 chars


# ── overlap validation ────────────────────────────────────────────────────────


def test_overlap_between_admin_and_proxy_keys_raises() -> None:
    """Shared key between admin and proxy planes must be rejected at boot."""
    shared_key = LONG_PROXY_KEY  # same key in both sets
    with pytest.raises(ValueError, match="overlap"):
        Settings(
            proxy_api_keys=shared_key,
            admin_api_keys=shared_key,
        )


def test_partial_overlap_raises() -> None:
    """One overlapping key in a comma-separated list is enough to raise."""
    extra_proxy = "extra-proxy-key-only-" + "x" * 19  # 40 chars, proxy-only
    overlap_key = LONG_PROXY_KEY
    with pytest.raises(ValueError, match="overlap"):
        Settings(
            proxy_api_keys=f"{extra_proxy},{overlap_key}",
            admin_api_keys=f"{LONG_ADMIN_KEY},{overlap_key}",
        )


def test_no_overlap_with_distinct_keys_does_not_raise() -> None:
    """Completely distinct key sets must not raise."""
    Settings(
        proxy_api_keys=LONG_PROXY_KEY,
        admin_api_keys=LONG_ADMIN_KEY,
    )


def test_empty_admin_keys_skips_overlap_check() -> None:
    """No admin keys → overlap check irrelevant → no error."""
    Settings(proxy_api_keys=LONG_PROXY_KEY, admin_api_keys="")


# ── production key-length validation ─────────────────────────────────────────


def test_production_short_admin_key_raises() -> None:
    """Production environment + admin key shorter than 40 chars → ValueError."""
    with pytest.raises(ValueError, match="40"):
        Settings(
            environment="production",
            proxy_api_keys=LONG_PROXY_KEY,
            admin_api_keys=SHORT_KEY,
        )


def test_production_short_admin_key_raises_for_prod_alias() -> None:
    """'prod' is an accepted alias for the production environment."""
    with pytest.raises(ValueError, match="40"):
        Settings(
            environment="prod",
            proxy_api_keys=LONG_PROXY_KEY,
            admin_api_keys=SHORT_KEY,
        )


def test_production_long_admin_key_no_overlap_is_valid() -> None:
    """Production + admin key >= 40 chars + no overlap → boot succeeds."""
    s = Settings(
        environment="production",
        proxy_api_keys=LONG_PROXY_KEY,
        admin_api_keys=LONG_ADMIN_KEY,
    )
    assert LONG_ADMIN_KEY in s.admin_keys


def test_development_short_admin_key_is_accepted() -> None:
    """Development environment ignores admin key length requirement."""
    s = Settings(
        environment="development",
        proxy_api_keys=LONG_PROXY_KEY,
        admin_api_keys=SHORT_KEY,
    )
    assert SHORT_KEY in s.admin_keys


def test_dev_environment_alias_short_key_accepted() -> None:
    """Explicit 'dev' environment also skips length validation."""
    s = Settings(
        environment="dev",
        proxy_api_keys=LONG_PROXY_KEY,
        admin_api_keys=SHORT_KEY,
    )
    assert SHORT_KEY in s.admin_keys


# ── admin_keys property ───────────────────────────────────────────────────────


def test_admin_keys_parsed_from_comma_separated_string() -> None:
    key_a = "admin-key-a-" + "a" * 28  # 40 chars
    key_b = "admin-key-b-" + "b" * 28  # 40 chars
    # Use a proxy key that differs from both admin keys
    proxy_k = "proxy-key-0-" + "0" * 28  # 40 chars
    s = Settings(proxy_api_keys=proxy_k, admin_api_keys=f"{key_a},{key_b}")
    assert s.admin_keys == {key_a, key_b}


def test_admin_keys_strips_whitespace_around_commas() -> None:
    proxy_k = "proxy-key-0-" + "0" * 28
    s = Settings(
        proxy_api_keys=proxy_k,
        admin_api_keys=f" {LONG_ADMIN_KEY} ",
    )
    assert LONG_ADMIN_KEY in s.admin_keys


def test_empty_admin_api_keys_gives_empty_set() -> None:
    s = Settings(proxy_api_keys=LONG_PROXY_KEY, admin_api_keys="")
    assert s.admin_keys == set()
