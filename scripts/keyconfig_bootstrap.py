"""KeyConfig bootstrap script for orkoprox.

Problem:
  All calls to orkoprox run via the static `PROXY_API_KEYS` list (legacy path).
  On a fresh Redis there are NO KeyConfigs registered (`KEYS metering:config:*`
  is empty). The per-tenant cost rollup endpoint
  `/v1/admin/metering/usage/{tenant_id}` returns `found: false` for every
  request. Per-tenant cost tracking is effectively dead.

Solution:
  Bootstrap one KeyConfig per key into Redis from the static keys
  (PROXY_API_KEYS). The tenant ID is derived from the optional key prefix:
    `<prefix>_<tenant_slug>_<random>` -> tenant_id=<tenant_slug>
    `<random>` (no prefix)            -> tenant_id="unknown"

Idempotent:
  A second run updates existing configs (set + json.dumps), deletes none.
  Requires REDIS_URL + PROXY_API_KEYS from env.

Cache invalidation:
  The web server process has a negative cache with a 30s TTL for lookups of
  non-existent keys. New keys become visible in the web server within <30s
  after a bootstrap run — NO container restart needed.

Usage:
  REDIS_URL=redis://redis:6379/0 \\
  PROXY_API_KEYS=key1,key2,key3 \\
    python scripts/keyconfig_bootstrap.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys

logger = logging.getLogger("keyconfig_bootstrap")

# Default per-key daily limit: conservative, can be adjusted per tenant.
# Below hard-cap so solo tenants are not blocked at trivial usage.
_DEFAULT_DAILY_TOKEN_LIMIT = 5_000_000  # 5M tokens/day ≈ $1.50/day at chat tier

# Tier quotas — monthly limits in tokens; daily ≈ monthly/30.
TIER_QUOTAS: dict[str, dict[str, int]] = {
    "solo": {"monthly": 10_000_000, "daily": 666_667},      # 10M/mo
    "starter": {"monthly": 10_000_000, "daily": 666_667},   # alias for solo (legacy)
    "business": {"monthly": 40_000_000, "daily": 2_666_667},  # 40M/mo
    "professional": {"monthly": 120_000_000, "daily": 8_000_000},  # 120M/mo
    "pro": {"monthly": 120_000_000, "daily": 8_000_000},    # alias for professional
    "enterprise": {"monthly": 400_000_000, "daily": 26_666_667},  # 400M/mo
}

# Optional: keys may carry a short prefix to derive tenant_id automatically.
# Pattern: <prefix>_<slug>_<random>  e.g. "acme_tenant1_abc123"
_KEY_PREFIX_RE = re.compile(r"^([a-z]+)_([a-z0-9-]+)_")


def derive_tenant_id(api_key: str) -> tuple[str, str]:
    """Derive tenant_id and package from an optional key prefix.

    Keys with pattern ``<prefix>_<slug>_<random>`` map to
    tenant_id=<slug> and package=<prefix>. Keys without a recognisable
    prefix fall back to tenant_id="unknown", package="legacy".

    Returns (tenant_id, package).
    """
    m = _KEY_PREFIX_RE.match(api_key)
    if not m:
        return "unknown", "legacy"
    prefix, slug = m.group(1), m.group(2)
    return slug, prefix


def resolve_tier_quota(
    package: str,
    tier_override: str | None = None,
    daily_fallback: int = _DEFAULT_DAILY_TOKEN_LIMIT,
) -> tuple[int, int]:
    """Resolve tier quota as (daily, monthly) token limits.

    Resolution order:
    1. tier_override (if explicitly set) -> TIER_QUOTAS lookup
    2. package -> TIER_QUOTAS lookup
    3. fallback: daily=daily_fallback, monthly=daily*30
    """
    key = (tier_override or package or "").lower()
    if key in TIER_QUOTAS:
        q = TIER_QUOTAS[key]
        return q["daily"], q["monthly"]
    return daily_fallback, daily_fallback * 30


def bootstrap(
    api_keys: list[str],
    *,
    redis_url: str,
    daily_token_limit: int,
    monthly_token_limit: int = 0,
    tier: str | None = None,
    dry_run: bool,
) -> dict[str, dict[str, object]]:
    """Write one KeyConfig into Redis per API key.

    If ``tier`` is set, daily/monthly limits are taken from TIER_QUOTAS
    (may be overridden by ``daily_token_limit`` / ``monthly_token_limit``).
    If only ``daily_token_limit`` is set, monthly = daily*30 is derived
    (backward-compat for legacy bootstrap).

    Returns: dict[api_key -> {tenant_id, package, daily_token_limit,
                              monthly_token_limit, already_existed: bool}]
    """
    from app.token_metering import KeyConfig, TokenMeteringService

    # Lazy redis import — script may run without Redis in --dry-run mode
    redis_client = None
    if not dry_run:
        import redis as _redis  # type: ignore[import-untyped]

        redis_client = _redis.Redis.from_url(redis_url, decode_responses=True)
        # Smoke-Check: ping
        redis_client.ping()
        logger.info("Connected to Redis: %s", redis_url)

    service = TokenMeteringService(redis_client)
    result: dict[str, dict[str, object]] = {}

    for api_key in api_keys:
        api_key = api_key.strip()
        if not api_key:
            continue

        tenant_id, package = derive_tenant_id(api_key)
        existing = service.get_key_config(api_key) if not dry_run else None

        # Tier resolution: explicit > package > fallback
        eff_daily, eff_monthly = resolve_tier_quota(
            package=package,
            tier_override=tier,
            daily_fallback=daily_token_limit,
        )
        # CLI overrides take priority when explicitly provided
        if monthly_token_limit > 0:
            eff_monthly = monthly_token_limit
        if daily_token_limit != _DEFAULT_DAILY_TOKEN_LIMIT:
            # User explicitly overrode the default
            eff_daily = daily_token_limit

        config = KeyConfig(
            tenant_id=tenant_id,
            daily_token_limit=eff_daily,
            monthly_token_limit=eff_monthly,
            package=package,
            active=True,
        )

        if dry_run:
            logger.info(
                "[DRY-RUN] would register key=%s tenant=%s package=%s daily=%d monthly=%d",
                api_key[:12] + "...",
                tenant_id,
                package,
                eff_daily,
                eff_monthly,
            )
        else:
            service.register_key(api_key, config)

        result[api_key] = {
            "tenant_id": tenant_id,
            "package": package,
            "daily_token_limit": eff_daily,
            "monthly_token_limit": eff_monthly,
            "already_existed": existing is not None,
        }

    return result


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Do not write anything")
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_DAILY_TOKEN_LIMIT,
        help="daily_token_limit per key (default 5M)",
    )
    parser.add_argument(
        "--monthly-limit",
        type=int,
        default=0,
        help="monthly_token_limit per key (default = tier quota or daily*30)",
    )
    parser.add_argument(
        "--tier",
        type=str,
        default=None,
        choices=list(TIER_QUOTAS.keys()),
        help="Tier override (solo/business/professional/enterprise) — sets daily+monthly from TIER_QUOTAS",
    )
    parser.add_argument(
        "--keys",
        type=str,
        default=None,
        help="Comma-separated keys (overrides PROXY_API_KEYS env var)",
    )
    args = parser.parse_args()

    raw_keys = args.keys or os.environ.get("PROXY_API_KEYS", "")
    if not raw_keys:
        print("ERROR: --keys or env PROXY_API_KEYS must be set", file=sys.stderr)
        return 2

    api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    if not api_keys:
        print("ERROR: no keys found after split.", file=sys.stderr)
        return 2

    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    if not args.dry_run and not redis_url:
        print("ERROR: REDIS_URL not set", file=sys.stderr)
        return 2

    result = bootstrap(
        api_keys,
        redis_url=redis_url,
        daily_token_limit=args.limit,
        monthly_token_limit=args.monthly_limit,
        tier=args.tier,
        dry_run=args.dry_run,
    )

    print(f"\n--- Bootstrap summary ({'DRY-RUN' if args.dry_run else 'LIVE'}) ---")
    new = sum(1 for v in result.values() if not v.get("already_existed"))
    upd = sum(1 for v in result.values() if v.get("already_existed"))
    print(f"  total: {len(result)}")
    print(f"  new:   {new}")
    print(f"  updated: {upd}")
    print()
    for k, v in result.items():
        marker = "UPD" if v["already_existed"] else "NEW"
        print(
            f"  [{marker}] {k[:16]}... -> tenant={v['tenant_id']} "
            f"pkg={v['package']} daily={v['daily_token_limit']} "
            f"monthly={v['monthly_token_limit']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
