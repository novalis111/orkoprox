"""Token Metering — Per-Key Usage Tracking & Budget Enforcement.

Tracks token consumption per API key with daily AND monthly budgets.
Stores usage in Redis with automatic daily + monthly reset.
Returns usage info via response headers for real-time client display.

Quota-Status-Stufen (KISS, fuer Soft-Warn-Pfade in Consumern):
- ``ok``        — < 80% des Limits.
- ``warn``      — 80% bis < 95%.
- ``critical``  — 95% bis < 100%.
- ``exceeded``  — >= 100%. Server liefert 429.

Status tiers are exposed via the ``{prefix}-Quota-Status`` header (prefix
configurable, default ``X-Orkoprox``). The reset time (next UTC midnight / 1st
of next month) is exposed via ``{prefix}-Quota-Reset``.

Monthly quota:
  Monthly limits act as the primary hard-stop; the daily limit is only a sanity
  brake (warn-only header, no hard-stop) to catch a runaway client script.
  KeyConfig carries ``monthly_token_limit`` (defaults to daily*30 if unset).
  ``check_budget`` enforces monthly as the primary hard-stop. Quota status is
  MAX(daily-pct, monthly-pct). Reset: daily 00:00 UTC, monthly 1st 00:00 UTC.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.storage import KeyValueStore

logger = logging.getLogger(__name__)

# Key config format in Redis: "metering:config:{api_key}" → JSON
# Daily usage format in Redis:   "metering:usage:{api_key}:{YYYY-MM-DD}" → JSON
# Monthly usage format in Redis: "metering:usage:{api_key}:{YYYY-MM}" → JSON
# TTL Daily: 7 days. TTL Monthly: 90 days (3 Months Audit-History).

USAGE_TTL_SECONDS = 7 * 86400  # 7 days retention (daily)
MONTHLY_USAGE_TTL_SECONDS = 90 * 86400  # 90 days retention (monthly history)
CONFIG_KEY_PREFIX = "metering:config"
USAGE_KEY_PREFIX = "metering:usage"

# Quota-Soft-Warn-Schwellen (in Prozent). Hard-Limit = 100% (429 vom Proxy).
QUOTA_WARN_PCT = 80
QUOTA_CRITICAL_PCT = 95

# If only daily_token_limit is set, derive monthly = daily * 30.
DEFAULT_DAILY_TO_MONTHLY_FACTOR = 30  # x30 for keys without an explicit monthly


def quota_status_for(pct: int) -> str:
    """Mappt Usage-Prozent auf einen Status-Bezeichner.

    Reine Funktion ohne Redis — fuer Tests + Consumer-Code wiederverwendbar.
    """
    if pct >= 100:
        return "exceeded"
    if pct >= QUOTA_CRITICAL_PCT:
        return "critical"
    if pct >= QUOTA_WARN_PCT:
        return "warn"
    return "ok"


def _next_utc_midnight_iso(now: datetime | None = None) -> str:
    """Naechste UTC-Mitternacht als ISO-8601-String (mit ``Z``)."""
    base = now or datetime.now(UTC)
    next_day = (base + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return next_day.isoformat().replace("+00:00", "Z")


def _next_month_first_iso(now: datetime | None = None) -> str:
    """Naechster 1. des Folgemonats 00:00 UTC als ISO-8601-String (mit ``Z``).

    Beispiel: now=2026-05-15 -> "2026-06-01T00:00:00Z"
    Edge-Case Dezember: now=2026-12-15 -> "2027-01-01T00:00:00Z"
    """
    base = now or datetime.now(UTC)
    if base.month == 12:
        next_month = base.replace(
            year=base.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        next_month = base.replace(
            month=base.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return next_month.isoformat().replace("+00:00", "Z")


def _current_month_key(now: datetime | None = None) -> str:
    """Aktueller Monats-Key im Format YYYY-MM."""
    base = now or datetime.now(UTC)
    return base.strftime("%Y-%m")


class KeyConfig:
    """Configuration for a metered API key.

    Budget model:
      ``monthly_token_limit`` is the primary hard-stop.
      ``daily_token_limit`` is a sanity brake (warn-only).
      Bei Legacy-Keys ohne explizites monthly: Default = daily * 30.
      Setting daily=0 + monthly>0 erlaubt unbegrenzten Tagesverbrauch
      bis Monatsbudget erschoepft.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        daily_token_limit: int = 0,
        monthly_token_limit: int = 0,
        package: str = "",
        active: bool = True,
    ) -> None:
        self.tenant_id = tenant_id
        self.daily_token_limit = daily_token_limit
        # Auto-derive monthly aus daily wenn nicht explizit gesetzt.
        # Sicherheits-Default damit Legacy-Keys nicht plotzlich ungebremst sind.
        if monthly_token_limit <= 0 and daily_token_limit > 0:
            self.monthly_token_limit = daily_token_limit * DEFAULT_DAILY_TO_MONTHLY_FACTOR
        else:
            self.monthly_token_limit = monthly_token_limit
        self.package = package
        self.active = active

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "daily_token_limit": self.daily_token_limit,
            "monthly_token_limit": self.monthly_token_limit,
            "package": self.package,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeyConfig:
        return cls(
            tenant_id=data["tenant_id"],
            daily_token_limit=int(data.get("daily_token_limit", 0)),
            monthly_token_limit=int(data.get("monthly_token_limit", 0)),
            package=data.get("package", ""),
            active=data.get("active", True),
        )


class DailyUsage:
    """Token + Image usage for a single day.

    Cost tracking: stored as USD microcents (USD * 1e6, int) to avoid
    floating-point drift across many calls.

    W3-Pre.3 (2026-05-04): zusaetzlich `image_count` fuer Image-Generation-
    Quota. Auch bei kostenlosen Image-Modellen (SDXL) wichtig fuer
    Per-Image-Rate-Limits und Audit-Trail.
    """

    def __init__(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        request_count: int = 0,
        cost_micro_usd: int = 0,
        image_count: int = 0,
    ) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.request_count = request_count
        # Microcents = USD * 1_000_000. Int storage statt float gegen Drift.
        self.cost_micro_usd = cost_micro_usd
        self.image_count = image_count

    @property
    def cost_usd(self) -> float:
        return self.cost_micro_usd / 1_000_000.0

    @property
    def cost_eur(self) -> float:
        from app.providers.ovh_pricing import USD_TO_EUR

        return self.cost_usd * USD_TO_EUR

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "request_count": self.request_count,
            "cost_micro_usd": self.cost_micro_usd,
            "image_count": self.image_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailyUsage:
        return cls(
            prompt_tokens=int(data.get("prompt_tokens", 0)),
            completion_tokens=int(data.get("completion_tokens", 0)),
            total_tokens=int(data.get("total_tokens", 0)),
            request_count=int(data.get("request_count", 0)),
            cost_micro_usd=int(data.get("cost_micro_usd", 0)),
            image_count=int(data.get("image_count", 0)),
        )


#: TTL for negative-cache entries.
#: Wenn ein Key nicht in Redis existiert, wird das fuer max. NEGATIVE_CACHE_TTL_S
#: gecached. Danach wird Redis erneut gefragt — laesst neue Keys aus
#: keyconfig_bootstrap.py-Cross-Process-Schreibern in <30s sichtbar werden,
#: ohne dass der Webserver-Prozess neugestartet werden muss. 30s ist Sweet-
#: Spot: lang genug zur Daempfung von Bot-Hammer auf ungueltige Keys, kurz
#: genug fuer reibungsloses Bootstrap-Workflow.
NEGATIVE_CACHE_TTL_S: float = 30.0


class TokenMeteringService:
    """Per-key token metering with Redis backend."""

    def __init__(
        self,
        redis_client: Any | None = None,
        header_prefix: str = "X-Orkoprox",
        store: "KeyValueStore | None" = None,
    ) -> None:
        # Storage backend: an explicit store wins; else a Redis client wraps to
        # RedisStore; else the Zero-Config in-memory fallback. Metering always
        # works — Redis is optional (state just resets on restart in-memory).
        from app.storage import MemoryStore, RedisStore

        if store is not None:
            self._store: KeyValueStore = store
        elif redis_client is not None:
            self._store = RedisStore(redis_client)
        else:
            self._store = MemoryStore()
        # Namespace for all usage/quota response headers (e.g. "X-Orkoprox" →
        # "X-Orkoprox-Usage-Pct"). Configurable so deployments can rebrand the
        # gateway's response headers without code changes.
        self._header_prefix = header_prefix.rstrip("-")
        # Positive-Cache: dauerhaft, wird via register_key() invalidiert wenn
        # der Webserver-Prozess selbst der Schreiber ist.
        self._config_cache: dict[str, KeyConfig] = {}
        # Negative-Cache: (api_key) -> monotonic_timestamp des Lookup-Misses.
        # Bei Lookup pruefen ob Eintrag aelter als NEGATIVE_CACHE_TTL_S — wenn
        # ja, Store erneut fragen (key existiert evtl. mittlerweile via Cross-
        # Process-Bootstrap).
        self._negative_cache: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        # Metering is always available now (in-memory fallback when no Redis).
        return True

    def register_key(self, api_key: str, config: KeyConfig) -> None:
        """Register or update a metered API key."""
        store_key = f"{CONFIG_KEY_PREFIX}:{api_key}"
        self._store.set_str(store_key, json.dumps(config.to_dict()))
        self._config_cache[api_key] = config
        # Negative-Cache invalidieren — wenn vorher ein Miss gecached war,
        # ist er jetzt obsolet (wir haben gerade den Key geschrieben).
        self._negative_cache.pop(api_key, None)
        logger.info(
            "Metering key registered: tenant=%s, daily=%d, monthly=%d",
            config.tenant_id,
            config.daily_token_limit,
            config.monthly_token_limit,
        )

    def revoke_key(self, api_key: str) -> None:
        """Deactivate a metered API key."""
        config = self.get_key_config(api_key)
        if config:
            config.active = False
            self.register_key(api_key, config)
        self._config_cache.pop(api_key, None)
        self._negative_cache.pop(api_key, None)

    def get_key_config(self, api_key: str) -> KeyConfig | None:
        """Get config for a key. Returns None for unmetered (legacy) keys.

        Cache-Layer:
        1. Positive-Cache (self._config_cache): Hit → sofortiger Return.
        2. Negative-Cache (self._negative_cache): Hit + nicht abgelaufen →
           sofortiger None-Return. Hit + abgelaufen → Eintrag wird verworfen,
           Redis erneut gefragt.
        3. Store-Miss → in Negative-Cache mit monotonic_timestamp.
        """
        if api_key in self._config_cache:
            return self._config_cache[api_key]
        # Negative-Cache mit TTL pruefen
        neg_ts = self._negative_cache.get(api_key)
        if neg_ts is not None:
            if (time.monotonic() - neg_ts) < NEGATIVE_CACHE_TTL_S:
                return None
            # TTL abgelaufen: Eintrag verwerfen, Store erneut fragen
            self._negative_cache.pop(api_key, None)

        store_key = f"{CONFIG_KEY_PREFIX}:{api_key}"
        raw = self._store.get_str(store_key)
        if not raw:
            self._negative_cache[api_key] = time.monotonic()
            return None
        config = KeyConfig.from_dict(json.loads(raw))
        self._config_cache[api_key] = config
        return config

    def check_budget(self, api_key: str) -> tuple[bool, KeyConfig | None, DailyUsage]:
        """Check if a key has budget remaining.

        Returns: (allowed, config, daily_usage)
        - allowed=True if request should proceed
        - config=None for unmetered keys (always allowed)

        Pricing-V4 (2026-05-04):
          Monthly-Limit ist primaerer Hard-Stop. Daily-Limit bleibt als
          optionale Sanity-Bremse. Wenn beide Limits gesetzt sind, blockt
          der Proxy sobald EINES erschoepft ist (429).
          Wenn nur monthly gesetzt: Daily ist unbegrenzt bis monthly leer.
          Wenn nur daily gesetzt (Legacy): bleibt wie vorher.
        """
        config = self.get_key_config(api_key)
        if config is None:
            return True, None, DailyUsage()
        if not config.active:
            return False, config, DailyUsage()
        # Beide Limits = 0 => unmetered Pass-Through (Test-Keys etc.)
        if config.daily_token_limit <= 0 and config.monthly_token_limit <= 0:
            return True, config, DailyUsage()

        usage = self.get_daily_usage(api_key)

        # Daily-Hard-Stop nur wenn explizit gesetzt
        if config.daily_token_limit > 0 and usage.total_tokens >= config.daily_token_limit:
            return False, config, usage

        # Monthly-Hard-Stop (Pricing-V4-Primary)
        if config.monthly_token_limit > 0:
            monthly = self.get_monthly_usage(api_key)
            if monthly.total_tokens >= config.monthly_token_limit:
                return False, config, usage

        return True, config, usage

    def record_usage(
        self,
        api_key: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str = "",
        provider: str = "",
        n_images: int = 0,
        audio_seconds: float = 0.0,
    ) -> DailyUsage:
        """Record token + image + audio usage for a request.

        Cost-Tracking in microcents (USD*1e6).

        Achsen:
        - Token (prompt+completion): cost_usd() aus OVH_MODEL_PRICING_USD
        - Images: ueber n_images-Achse (aktuell SDXL kostenlos)
        - Audio (Whisper): audio_cost_usd() aus OVH_AUDIO_PRICING_USD
          (`/v1/models` zeigt 0, echte Achse `seconds` kommt nur in der
          OVH-Rechnung — Werte aus Rechnung 2026-05-05 hinterlegt)

        Wenn provider="ovh" ODER Modell in OVH_MODEL_PRICING_USD/
        OVH_AUDIO_PRICING_USD steht, berechnen wir Microcents und
        persistieren sie im Store. Microcents auf naechsten microcent
        aufgerundet, damit Sub-Penny-Calls nicht im 0-Drift verschwinden.
        """
        now = datetime.now(UTC)
        today = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")
        daily_store_key = f"{USAGE_KEY_PREFIX}:{api_key}:{today}"
        monthly_store_key = f"{USAGE_KEY_PREFIX}:{api_key}:{month}"

        from app.providers.ovh_pricing import (
            OVH_AUDIO_PRICING_USD,
            OVH_MODEL_PRICING_USD,
            audio_cost_usd,
            billable_tokens as ovh_billable_tokens,
            cost_usd as ovh_cost_usd,
        )
        from app.providers.mistral_lp_pricing import (
            billable_tokens as mistral_lp_billable_tokens,
            cost_usd as mistral_lp_cost_usd,
            is_priced as mistral_lp_is_priced,
        )

        # Provider routing for token weighting + cost tracking.
        # `total_tokens` = weighted virtual tokens for quota accounting.
        # `prompt_tokens` + `completion_tokens` stay raw (audit/debug).
        # The Mistral-LP provider path uses its own pricing map
        # (mistral_lp_pricing.py): premium models draw proportionally more
        # virtual tokens than the cheaper default models.
        is_mistral_lp = provider == "mistral_lp" or mistral_lp_is_priced(model)
        if is_mistral_lp:
            total = mistral_lp_billable_tokens(
                model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        else:
            total = ovh_billable_tokens(
                model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                audio_seconds=audio_seconds,
            )

        microcents = 0
        is_ovh = (
            provider == "ovh"
            or model in OVH_MODEL_PRICING_USD
            or model in OVH_AUDIO_PRICING_USD
        )
        if is_mistral_lp:
            usd = mistral_lp_cost_usd(model, prompt_tokens, completion_tokens)
            import math
            microcents = max(1, math.ceil(usd * 1_000_000)) if usd > 0 else 0
        elif is_ovh:
            usd = ovh_cost_usd(
                model,
                prompt_tokens,
                completion_tokens,
                n_images=n_images,
                n_requests=1,
            )
            usd += audio_cost_usd(model, audio_seconds)
            import math

            microcents = max(1, math.ceil(usd * 1_000_000)) if usd > 0 else 0

        # Common counter increments for both the daily and monthly windows.
        fields: dict[str, int] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total,
            "request_count": 1,
        }
        if n_images > 0:
            fields["image_count"] = n_images
        if microcents > 0:
            fields["cost_micro_usd"] = microcents

        # Atomic per-key increment + TTL refresh; returns the full counter map.
        daily = self._store.incr_fields(daily_store_key, fields, USAGE_TTL_SECONDS)
        self._store.incr_fields(monthly_store_key, fields, MONTHLY_USAGE_TTL_SECONDS)

        usage = DailyUsage(
            prompt_tokens=daily.get("prompt_tokens", 0),
            completion_tokens=daily.get("completion_tokens", 0),
            total_tokens=daily.get("total_tokens", 0),
            request_count=daily.get("request_count", 0),
            cost_micro_usd=daily.get("cost_micro_usd", 0),
            image_count=daily.get("image_count", 0),
        )

        config = self.get_key_config(api_key)
        tenant_id = config.tenant_id if config else "unknown"
        logger.debug(
            "Token usage: tenant=%s provider=%s model=%s prompt=%d completion=%d "
            "total_day=%d cost_eur_day=%.4f",
            tenant_id,
            provider,
            model,
            prompt_tokens,
            completion_tokens,
            usage.total_tokens,
            usage.cost_eur,
        )
        return usage

    def get_daily_usage(self, api_key: str, date: str | None = None) -> DailyUsage:
        """Get usage for a specific day (default: today)."""
        if date is None:
            date = datetime.now(UTC).strftime("%Y-%m-%d")
        store_key = f"{USAGE_KEY_PREFIX}:{api_key}:{date}"
        raw = self._store.get_fields(store_key)
        if not raw:
            return DailyUsage()
        return DailyUsage.from_dict(raw)

    def get_monthly_usage(self, api_key: str, month: str | None = None) -> DailyUsage:
        """Get usage aggregate for a specific month (default: current).

        ``month`` Format: YYYY-MM. Reset 1. des Folgemonats 00:00 UTC.
        Reuse von DailyUsage als Container-Klasse — semantisch ist es ein
        Monats-Aggregat, aber alle Felder identisch (tokens, cost, images).
        """
        if month is None:
            month = _current_month_key()
        store_key = f"{USAGE_KEY_PREFIX}:{api_key}:{month}"
        raw = self._store.get_fields(store_key)
        if not raw:
            return DailyUsage()
        return DailyUsage.from_dict(raw)

    def get_usage_range(self, api_key: str, days: int = 7) -> dict[str, DailyUsage]:
        """Get usage for the last N days."""
        result = {}
        today = datetime.now(UTC)
        for i in range(days):
            from datetime import timedelta

            date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            usage = self.get_daily_usage(api_key, date)
            if usage.total_tokens > 0 or usage.request_count > 0:
                result[date] = usage
        return result

    def build_usage_headers(
        self,
        config: KeyConfig | None,
        usage: DailyUsage,
        monthly: DailyUsage | None = None,
        api_key: str | None = None,
    ) -> dict[str, str]:
        """Build response headers with usage info for client display.

        Header names are namespaced by ``self._header_prefix`` (default
        ``X-Orkoprox``). In addition to the Used/Limit/Pct headers we emit:
        - ``{prefix}-Quota-Status`` (ok | warn | critical | exceeded)
          — takes MAX(daily-pct, monthly-pct)
        - ``{prefix}-Quota-Reset`` (next UTC midnight, ISO-8601)
        - ``{prefix}-Tokens-Used-Month`` / ``{prefix}-Token-Limit-Month``
          / ``{prefix}-Usage-Pct-Month`` / ``{prefix}-Quota-Reset-Month``
        - ``{prefix}-Daily-Pct`` (warn-only, daily sanity limit)

        If ``monthly`` is not passed and ``api_key`` is set, monthly usage is
        loaded on demand.
        """
        if config is None:
            return {}
        # Monthly bei Bedarf nachladen (lazy)
        if monthly is None and api_key is not None and config.monthly_token_limit > 0:
            monthly = self.get_monthly_usage(api_key)
        if monthly is None:
            monthly = DailyUsage()

        daily_limit = config.daily_token_limit
        monthly_limit = config.monthly_token_limit
        used_today = usage.total_tokens
        used_month = monthly.total_tokens

        # Pct-Berechnung pro Achse
        daily_pct = min(100, int((used_today / daily_limit * 100) if daily_limit > 0 else 0))
        monthly_pct = min(100, int((used_month / monthly_limit * 100) if monthly_limit > 0 else 0))

        # Combined-Pct = max(daily, monthly) — beides relevant.
        # Wenn nur monthly gesetzt: combined = monthly_pct (daily=0).
        combined_pct = max(daily_pct, monthly_pct)
        if daily_limit <= 0 and monthly_limit <= 0:
            status = "ok"
        else:
            status = quota_status_for(combined_pct)

        p = self._header_prefix
        headers = {
            # Daily
            f"{p}-Tokens-Used-Today": str(used_today),
            f"{p}-Token-Limit": str(daily_limit),
            f"{p}-Usage-Pct": str(combined_pct),  # max(daily, monthly)
            f"{p}-Daily-Pct": str(daily_pct),  # daily-only
            f"{p}-Tenant-Id": config.tenant_id,
            f"{p}-Quota-Status": status,
            f"{p}-Quota-Reset": _next_utc_midnight_iso(),
            f"{p}-Cost-EUR-Today": f"{usage.cost_eur:.4f}",
            f"{p}-Cost-USD-Today": f"{usage.cost_usd:.4f}",
            f"{p}-Images-Today": str(usage.image_count),
            # Monthly
            f"{p}-Tokens-Used-Month": str(used_month),
            f"{p}-Token-Limit-Month": str(monthly_limit),
            f"{p}-Usage-Pct-Month": str(monthly_pct),
            f"{p}-Quota-Reset-Month": _next_month_first_iso(),
            f"{p}-Cost-EUR-Month": f"{monthly.cost_eur:.4f}",
            f"{p}-Cost-USD-Month": f"{monthly.cost_usd:.4f}",
        }
        return headers

    def list_all_keys(self) -> list[dict[str, Any]]:
        """List all registered metered keys with current usage."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        keys = []
        prefix = f"{CONFIG_KEY_PREFIX}:"
        for store_key in self._store.scan_prefix(prefix):
            api_key = store_key[len(prefix):]
            config = self.get_key_config(api_key)
            if config:
                usage = self.get_daily_usage(api_key, today)
                monthly = self.get_monthly_usage(api_key)
                keys.append(
                    {
                        "api_key_prefix": api_key[:12] + "...",
                        "tenant_id": config.tenant_id,
                        "package": config.package,
                        "active": config.active,
                        "daily_token_limit": config.daily_token_limit,
                        "monthly_token_limit": config.monthly_token_limit,
                        "tokens_used_today": usage.total_tokens,
                        "tokens_used_month": monthly.total_tokens,
                        "cost_eur_month": monthly.cost_eur,
                        "requests_today": usage.request_count,
                        "requests_month": monthly.request_count,
                        "usage_pct": min(
                            100,
                            int(
                                (usage.total_tokens / config.daily_token_limit * 100)
                                if config.daily_token_limit > 0
                                else 0
                            ),
                        ),
                        "monthly_usage_pct": min(
                            100,
                            int(
                                (monthly.total_tokens / config.monthly_token_limit * 100)
                                if config.monthly_token_limit > 0
                                else 0
                            ),
                        ),
                    }
                )
        return keys
