"""Admin-plane authentication — strictly separate from the data plane.

The admin plane (/v1/admin/*: key management, tenant usage) is guarded by a
dedicated set of keys (ADMIN_API_KEYS). This module enforces two invariants
that close the most dangerous failure mode for a key-holding gateway:

1. A data-plane key (PROXY_API_KEYS or a metered key) can NEVER authenticate on
   the admin plane — so a leaked client key cannot mint or revoke keys.
2. Secure by default: if no admin keys are configured, the admin plane is
   DISABLED (403), not open.

Config validation (config.py) additionally guarantees admin keys never overlap
with data-plane keys.
"""

from __future__ import annotations

import hmac

from app.config import Settings
from app.errors import ProxyError


def _extract_key(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip() or None
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def matches_any(candidate: str, allowed: set[str]) -> bool:
    # Constant-time comparison against each allowed key to avoid leaking key
    # length/prefix through timing.
    matched = False
    for key in allowed:
        if hmac.compare_digest(candidate, key):
            matched = True
    return matched


# Private alias kept for existing internal callers within this module.
_matches_any = matches_any


def enforce_admin_auth(
    settings: Settings,
    x_api_key: str | None,
    authorization: str | None,
) -> str:
    """Authorise an admin-plane request. Returns the admin key on success.

    Raises ProxyError(403) if the admin plane is disabled, or ProxyError(401)
    if the presented key is missing or not a valid admin key. A data-plane key
    is rejected with 403 even if otherwise valid — never with a hint that it is
    "almost" an admin key.
    """
    admin_keys = settings.admin_keys
    if not admin_keys:
        raise ProxyError(
            http_status=403,
            code="admin_plane_disabled",
            message=(
                "admin plane is disabled — set ADMIN_API_KEYS to enable "
                "/v1/admin endpoints"
            ),
        )
    presented = _extract_key(x_api_key, authorization)
    if not presented:
        raise ProxyError(
            http_status=401, code="missing_admin_key", message="missing_admin_key"
        )
    if not _matches_any(presented, admin_keys):
        # Whether the key is a valid data-plane key or pure garbage, the answer
        # is the same: not authorised for the admin plane.
        raise ProxyError(
            http_status=403,
            code="admin_forbidden",
            message="key is not authorised for the admin plane",
        )
    return presented
