"""Production provider allowlist (PRODUCTION_ALLOWED_PROVIDERS).

By default the gateway places no restriction on which provider may be primary,
even in production. If PRODUCTION_ALLOWED_PROVIDERS is set, boot fails when the
primary provider is not on the list — useful to pin a deployment to
data-residency-compliant providers.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "environment": "development",
        "primary_provider": "ovh",
        "proxy_auth_required": False,
        "fallback_providers": "ovh",
        "fallback_providers_default": "ovh",
        "fallback_providers_chat": "ovh",
    }
    base.update(overrides)
    return Settings(**base)


def test_dev_environment_allows_any_primary_provider():
    """In dev there is never a boot block, regardless of the allowlist."""
    s = _settings(
        environment="development",
        primary_provider="some_provider",
        production_allowed_providers="ovh",
    )
    assert s.primary_provider == "some_provider"


def test_prod_without_allowlist_allows_any_primary_provider():
    """No allowlist set → no restriction, even in production."""
    s = _settings(environment="production", primary_provider="some_provider")
    assert s.primary_provider == "some_provider"


def test_prod_with_allowlisted_primary_boots_clean():
    s = _settings(
        environment="production",
        primary_provider="ovh",
        production_allowed_providers="ovh,mistral_lp",
    )
    assert s.primary_provider == "ovh"


def test_prod_with_non_allowlisted_primary_fails_boot():
    """Allowlist set + primary not on it → boot is blocked."""
    with pytest.raises(ValueError, match="PRODUCTION_ALLOWED_PROVIDERS"):
        _settings(
            environment="production",
            primary_provider="some_provider",
            production_allowed_providers="ovh,mistral_lp",
        )
