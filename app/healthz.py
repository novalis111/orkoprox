"""Enterprise-Grade Health-Probing fuer alle Backends.

Prinzip:
- Parallel-Probes mit kurzem Timeout (1-2s pro Backend).
- Redis-basierter Cache (10s TTL) verhindert Check-Storm bei n-Client-Cron.
- Status pro Backend: ok / degraded / unavailable / disabled.
- Gesamtstatus: ok (alle Pflicht-Backends ok) / degraded (ein Fallback kaputt) / critical (alles kaputt).

Dieser Endpoint ist Single Source of Truth fuer alle Clients.
Clients pingen /v1/healthz statt /v1/chat/completions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Cache-Key
_CACHE_KEY = "llm_proxy:healthz:status"
_CACHE_TTL_SECONDS = 10


@dataclass(frozen=True)
class BackendProbe:
    name: str
    url: str
    method: str = "GET"
    headers: dict[str, str] | None = None
    timeout_s: float = 2.0
    critical: bool = False  # Wenn True, macht ein Fehler den Gesamtstatus critical
    disabled: bool = False
    disabled_reason: str = ""


def _build_probes(settings: Any) -> list[BackendProbe]:
    """Baue Probe-Liste aus Settings. Nur konfigurierte Backends werden probed."""
    probes: list[BackendProbe] = []
    if settings.ovh_api_key:
        probes.append(
            BackendProbe(
                name="ovh",
                url=f"{settings.ovh_base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {settings.ovh_api_key}"},
                timeout_s=6.0,
                critical=True,
            )
        )
    else:
        probes.append(
            BackendProbe(
                name="ovh",
                url="",
                disabled=True,
                disabled_reason="OVH_API_KEY nicht gesetzt",
            )
        )
    return probes


async def _probe_one(client: httpx.AsyncClient, probe: BackendProbe) -> dict[str, Any]:
    """Einzelprobe. Liefert immer ein Ergebnis, wirft nie."""
    if probe.disabled:
        return {
            "name": probe.name,
            "status": "disabled",
            "detail": probe.disabled_reason,
        }
    start = time.monotonic()
    try:
        resp = await client.request(
            probe.method,
            probe.url,
            headers=probe.headers or {},
            timeout=probe.timeout_s,
        )
        latency_ms = round((time.monotonic() - start) * 1000)
        # Accept any 2xx/4xx (auth wrong ist erreichbar, Upstream lebt).
        # 401 haben wir bei ungueltigem Key -> separat markieren.
        if resp.status_code == 401:
            return {
                "name": probe.name,
                "status": "error",
                "detail": "API-Key ungueltig (HTTP 401)",
                "latency_ms": latency_ms,
            }
        if 200 <= resp.status_code < 500:
            return {
                "name": probe.name,
                "status": "ok",
                "http_status": resp.status_code,
                "latency_ms": latency_ms,
            }
        return {
            "name": probe.name,
            "status": "degraded",
            "detail": f"HTTP {resp.status_code}",
            "latency_ms": latency_ms,
        }
    except httpx.TimeoutException:
        return {
            "name": probe.name,
            "status": "unavailable",
            "detail": f"Timeout nach {probe.timeout_s}s",
            "latency_ms": round((time.monotonic() - start) * 1000),
        }
    except httpx.RequestError as exc:
        return {
            "name": probe.name,
            "status": "unavailable",
            "detail": f"Request-Fehler: {type(exc).__name__}",
            "latency_ms": round((time.monotonic() - start) * 1000),
        }
    except Exception as exc:  # defensive: nie den Endpoint killen
        logger.exception("Unerwarteter Fehler in Backend-Probe %s", probe.name)
        return {
            "name": probe.name,
            "status": "unavailable",
            "detail": f"Interner Fehler: {type(exc).__name__}",
            "latency_ms": round((time.monotonic() - start) * 1000),
        }


def _compute_overall(backends: list[dict[str, Any]]) -> str:
    """Gesamtstatus aus Einzelchecks ableiten.

    ok: alle critical == ok, sonst mindestens ein non-critical ok
    degraded: mindestens ein critical ist degraded/unavailable, aber mindestens ein anderer critical ok
             ODER non-critical degraded
    critical: alle critical kaputt -> Proxy ist nicht handlungsfaehig
    """
    critical_statuses = [b for b in backends if b.get("_critical")]
    critical_ok = [b for b in critical_statuses if b["status"] == "ok"]

    if not critical_statuses:
        # Kein critical-Backend konfiguriert -> ok wenn mindestens ein non-critical ok.
        if any(b["status"] == "ok" for b in backends):
            return "ok"
        return "degraded"

    if not critical_ok:
        return "critical"
    if len(critical_ok) < len(critical_statuses):
        return "degraded"
    # Alle critical ok. Non-critical pruefen:
    non_critical = [b for b in backends if not b.get("_critical") and b["status"] not in ("ok", "disabled")]
    if non_critical:
        return "degraded"
    return "ok"


async def probe_all_backends(settings: Any) -> dict[str, Any]:
    """Probiere alle Backends parallel. Kein Cache."""
    probes = _build_probes(settings)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[_probe_one(client, p) for p in probes],
            return_exceptions=False,
        )
    # Critical-Markierung fuer Overall-Berechnung
    for probe, result in zip(probes, results, strict=True):
        result["_critical"] = probe.critical
    overall = _compute_overall(list(results))
    # _critical ist internes Feld, nicht ausliefern
    public_backends = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
    return {
        "status": overall,
        "backends": public_backends,
        "cache": "miss",
    }


async def get_healthz(settings: Any, redis_client: Any | None = None) -> dict[str, Any]:
    """Cached Version. Liefert Healthz aus Cache oder probiert neu."""
    if redis_client is not None:
        try:
            cached = await redis_client.get(_CACHE_KEY)
            if cached:
                data = json.loads(cached) if isinstance(cached, (str, bytes)) else cached
                if isinstance(data, dict):
                    data["cache"] = "hit"
                    return data
        except Exception as exc:
            # Cache-Fehler darf Healthz nicht killen
            logger.warning("Healthz-Cache-Read fehlgeschlagen: %s", exc)

    result = await probe_all_backends(settings)

    if redis_client is not None:
        try:
            await redis_client.setex(_CACHE_KEY, _CACHE_TTL_SECONDS, json.dumps(result))
        except Exception as exc:
            logger.warning("Healthz-Cache-Write fehlgeschlagen: %s", exc)

    return result
