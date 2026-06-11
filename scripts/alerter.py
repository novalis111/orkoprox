#!/usr/bin/env python3
"""Enterprise-grade alerter for orkoprox.

Runs as a cron/loop job (every 60s). Checks:
  1. /v1/healthz -> backend status. A backend "down" for >= N consecutive
     checks triggers a Telegram alert. Recovery only after M consecutive OK.
  2. /metrics -> fallback rate over the LAST interval (counter delta),
     not lifetime rate. Threshold breach triggers an alert.

Alert lifecycle per alert kind:
  inactive -> (condition met) -> active  [alert sent]
  active   -> (condition gone + recovery threshold) -> recovery  [alert sent]
  recovery -> inactive

=> NO re-fires while a state persists. NO recovery without a prior real alert.
   NO flapping spam via recovery-consecutive counter.

State between runs stored in Redis. No Docker container, no daemon.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Any

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. pip install httpx", file=sys.stderr)
    sys.exit(2)

try:
    import redis
except ImportError:
    print("ERROR: redis not installed. pip install redis", file=sys.stderr)
    sys.exit(2)


PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:8081").rstrip("/")
PROXY_API_KEY = os.environ.get("PROXY_INTERNAL_API_KEY", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

TELEGRAM_BOT_TOKEN = os.environ.get("ALERT_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("ALERT_TELEGRAM_CHAT_ID", "")

DOWN_CONSEC_FAILS = int(os.environ.get("ALERT_BACKEND_DOWN_CONSECUTIVE_FAILS", "2"))
RECOVERY_CONSEC_OK = int(os.environ.get("ALERT_RECOVERY_CONSECUTIVE_OK", "3"))
P95_THRESHOLD_MS = int(os.environ.get("ALERT_P95_LATENCY_THRESHOLD_MS", "30000"))
FALLBACK_THRESHOLD = float(os.environ.get("ALERT_FALLBACK_RATE_THRESHOLD", "0.2"))
FALLBACK_MIN_DELTA_REQUESTS = int(os.environ.get("ALERT_FALLBACK_MIN_DELTA_REQUESTS", "50"))
FALLBACK_WINDOW_CONSEC = int(os.environ.get("ALERT_FALLBACK_WINDOW_CONSECUTIVE", "3"))
STATE_TTL_SECONDS = int(os.environ.get("ALERT_STATE_TTL_SECONDS", "86400"))

# Redis-Keys
KEY_CONSEC_FAIL = "alerter:backend_consec_fails"   # Hash: backend -> int
KEY_CONSEC_OK = "alerter:backend_consec_ok"        # Hash: backend -> int
KEY_ACTIVE = "alerter:active"                      # Hash: alert_kind -> "1"
KEY_METRICS_SNAPSHOT = "alerter:metrics_snapshot"  # Hash: requests, fallback, ts
KEY_FALLBACK_CONSEC = "alerter:fallback_consec"    # String: int


@dataclass
class AlertState:
    rc: "redis.Redis"

    # --- Backend consecutive counters -------------------------------------
    def get_consec_fail(self, backend: str) -> int:
        val = self.rc.hget(KEY_CONSEC_FAIL, backend)
        return int(val) if val else 0

    def incr_consec_fail(self, backend: str) -> int:
        n = int(self.rc.hincrby(KEY_CONSEC_FAIL, backend, 1))
        self.rc.expire(KEY_CONSEC_FAIL, STATE_TTL_SECONDS)
        return n

    def reset_consec_fail(self, backend: str) -> None:
        self.rc.hdel(KEY_CONSEC_FAIL, backend)

    def incr_consec_ok(self, backend: str) -> int:
        n = int(self.rc.hincrby(KEY_CONSEC_OK, backend, 1))
        self.rc.expire(KEY_CONSEC_OK, STATE_TTL_SECONDS)
        return n

    def reset_consec_ok(self, backend: str) -> None:
        self.rc.hdel(KEY_CONSEC_OK, backend)

    # --- Active-alert lifecycle (kein Re-Fire) ----------------------------
    def is_active(self, alert_kind: str) -> bool:
        return bool(self.rc.hget(KEY_ACTIVE, alert_kind))

    def mark_active(self, alert_kind: str) -> None:
        self.rc.hset(KEY_ACTIVE, alert_kind, "1")
        self.rc.expire(KEY_ACTIVE, STATE_TTL_SECONDS)

    def clear_active(self, alert_kind: str) -> None:
        self.rc.hdel(KEY_ACTIVE, alert_kind)

    # --- Fallback-Window --------------------------------------------------
    def incr_fallback_consec(self) -> int:
        n = int(self.rc.incr(KEY_FALLBACK_CONSEC))
        self.rc.expire(KEY_FALLBACK_CONSEC, STATE_TTL_SECONDS)
        return n

    def reset_fallback_consec(self) -> None:
        self.rc.delete(KEY_FALLBACK_CONSEC)

    # --- Metrics-Snapshot (Counter-Delta-Basis) ---------------------------
    def get_metrics_snapshot(self) -> dict[str, float] | None:
        snap = self.rc.hgetall(KEY_METRICS_SNAPSHOT)
        if not snap:
            return None
        try:
            return {
                "total_requests": float(snap.get("total_requests", 0)),
                "total_fallback_hits": float(snap.get("total_fallback_hits", 0)),
                "ts": float(snap.get("ts", 0)),
            }
        except (TypeError, ValueError):
            return None

    def put_metrics_snapshot(self, total_requests: float, total_fallback_hits: float) -> None:
        self.rc.hset(
            KEY_METRICS_SNAPSHOT,
            mapping={
                "total_requests": str(total_requests),
                "total_fallback_hits": str(total_fallback_hits),
                "ts": str(time.time()),
            },
        )
        self.rc.expire(KEY_METRICS_SNAPSHOT, STATE_TTL_SECONDS)


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[alerter] Telegram not configured, would have sent: {text[:200]}")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = httpx.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as exc:
        print(f"[alerter] Telegram send error: {exc}", file=sys.stderr)
        return False


class HealthzFetchResult:
    __slots__ = ("data", "kind", "detail")

    def __init__(self, kind: str, data: dict[str, Any] | None = None, detail: str = ""):
        self.data = data
        self.kind = kind  # "ok" | "auth_failed" | "unexpected_status" | "unreachable"
        self.detail = detail


def fetch_healthz() -> HealthzFetchResult:
    url = f"{PROXY_URL}/v1/healthz?no_cache=true"
    headers = {}
    if PROXY_API_KEY:
        headers["X-API-Key"] = PROXY_API_KEY
    try:
        resp = httpx.get(url, headers=headers, timeout=15)
    except Exception as exc:
        print(f"[alerter] /v1/healthz request error: {exc}", file=sys.stderr)
        return HealthzFetchResult("unreachable", detail=str(exc)[:200])

    if resp.status_code in (200, 503):
        try:
            return HealthzFetchResult("ok", data=resp.json())
        except Exception as exc:
            print(f"[alerter] /v1/healthz JSON-Parse-Fehler: {exc}", file=sys.stderr)
            return HealthzFetchResult(
                "unexpected_status",
                detail=f"HTTP {resp.status_code} but JSON parse error: {exc!s:.100}",
            )

    if resp.status_code == 401:
        print("[alerter] /v1/healthz HTTP 401 — alerter API key not in PROXY_API_KEYS", file=sys.stderr)
        return HealthzFetchResult(
            "auth_failed",
            detail="Alerter API key rejected (HTTP 401). Check PROXY_API_KEYS.",
        )

    print(f"[alerter] /v1/healthz HTTP {resp.status_code}", file=sys.stderr)
    return HealthzFetchResult(
        "unexpected_status",
        detail=f"HTTP {resp.status_code} (proxy is alive but responded unexpectedly).",
    )


def fetch_metrics() -> dict[str, float]:
    url = f"{PROXY_URL}/metrics"
    out: dict[str, float] = {"total_requests": 0.0, "total_fallback_hits": 0.0}
    try:
        resp = httpx.get(url, timeout=10)
        if resp.status_code != 200:
            return out
        for line in resp.text.splitlines():
            if not line or line.startswith("#"):
                continue
            if line.startswith("llm_proxy_requests_total{"):
                try:
                    out["total_requests"] += float(line.rsplit(" ", 1)[-1])
                except ValueError:
                    pass
            elif line.startswith("llm_proxy_fallback_hits_total{"):
                try:
                    out["total_fallback_hits"] += float(line.rsplit(" ", 1)[-1])
                except ValueError:
                    pass
    except Exception as exc:
        print(f"[alerter] /metrics request error: {exc}", file=sys.stderr)
    return out


def _fire_once(state: AlertState, kind: str, text: str) -> None:
    """Send alert only if state is not already active. Idempotent."""
    if state.is_active(kind):
        return
    send_telegram(text)
    state.mark_active(kind)


def _clear_and_recover(state: AlertState, kind: str, recovery_text: str) -> None:
    """Send recovery only if the alert was previously active."""
    if not state.is_active(kind):
        return
    send_telegram(recovery_text)
    state.clear_active(kind)


def check_backends(hz: dict[str, Any], state: AlertState) -> None:
    """Check backend status with flap dampening.

    - DOWN: fires once after >= DOWN_CONSEC_FAILS consecutive failures; active
      flag set, no further alerts while down.
    - RECOVERY: only fires when >= RECOVERY_CONSEC_OK consecutive OK checks AND
      an active alert exists.
    """
    backends = hz.get("backends", [])
    overall = hz.get("status", "unknown")
    for b in backends:
        name = b.get("name", "unknown")
        status = b.get("status", "unknown")
        kind = f"backend_down:{name}"

        if status in ("ok", "disabled"):
            state.reset_consec_fail(name)
            if state.is_active(kind):
                ok_count = state.incr_consec_ok(name)
                if ok_count >= RECOVERY_CONSEC_OK:
                    _clear_and_recover(
                        state,
                        kind,
                        f"*[orkoprox] RECOVERY* `{name}` ist wieder stabil up "
                        f"({ok_count} OK in Folge)\nOverall: `{overall}`",
                    )
                    state.reset_consec_ok(name)
            else:
                state.reset_consec_ok(name)
            continue

        # down
        state.reset_consec_ok(name)
        count = state.incr_consec_fail(name)
        if count >= DOWN_CONSEC_FAILS:
            detail = b.get("detail", "")
            latency_ms = b.get("latency_ms")
            _fire_once(
                state,
                kind,
                f"*[orkoprox] BACKEND DOWN* `{name}` ({count}x in Folge)\n"
                f"Status: `{status}`\n"
                f"Detail: `{detail[:200]}`\n"
                f"Latency: `{latency_ms}ms`\n"
                f"Overall: `{overall}`",
            )


def check_fallback_rate(metrics: dict[str, float], state: AlertState) -> None:
    """Check fallback rate over DELTA since last snapshot, not lifetime.

    Prevents historical errors from permanently poisoning the Prometheus counter
    and triggering sustained alerts.

    - Sample guard: only evaluate when delta-requests >= FALLBACK_MIN_DELTA_REQUESTS.
    - Window guard: only fire when rate exceeds threshold for FALLBACK_WINDOW_CONSEC
      consecutive runs (flap dampening for short spikes).
    - Re-fire guard: no further alert while one is already active.
    - Recovery: fires on first run back under threshold when an alert was active.
    """
    kind = "fallback_rate"
    total_req = metrics.get("total_requests", 0.0)
    total_fb = metrics.get("total_fallback_hits", 0.0)

    prev = state.get_metrics_snapshot()
    state.put_metrics_snapshot(total_req, total_fb)

    if prev is None:
        # Erster Run -> nur Snapshot setzen, nichts werten
        return

    d_req = total_req - prev["total_requests"]
    d_fb = total_fb - prev["total_fallback_hits"]

    # Counter reset (proxy restarted) -> negative delta, discard window
    if d_req < 0 or d_fb < 0:
        state.reset_fallback_consec()
        return

    if d_req < FALLBACK_MIN_DELTA_REQUESTS:
        # Sample too small -> do not count window, but also do not reset
        return

    rate = d_fb / d_req if d_req > 0 else 0.0

    if rate >= FALLBACK_THRESHOLD:
        consec = state.incr_fallback_consec()
        if consec >= FALLBACK_WINDOW_CONSEC:
            _fire_once(
                state,
                kind,
                f"*[orkoprox] FALLBACK-RATE HOCH* {rate:.1%} (letztes Fenster)\n"
                f"Threshold: `{FALLBACK_THRESHOLD:.0%}`\n"
                f"Delta-Requests: `{int(d_req)}` / Delta-Fallback-Hits: `{int(d_fb)}`\n"
                f"Fenster: `{consec}` Runs in Folge ueber Threshold.\n"
                f"Primary provider not responding — check backend health.",
            )
        return

    # Rate wieder unter Threshold
    state.reset_fallback_consec()
    _clear_and_recover(
        state,
        kind,
        f"*[orkoprox] RECOVERY* Fallback-Rate wieder normal: {rate:.1%} "
        f"(letztes Fenster, {int(d_req)} Requests).",
    )


def main() -> int:
    try:
        rc = redis.from_url(REDIS_URL, decode_responses=True)
        rc.ping()
    except Exception as exc:
        print(f"[alerter] Redis connect failed: {exc}", file=sys.stderr)
        return 2

    state = AlertState(rc=rc)

    result = fetch_healthz()

    if result.kind == "unreachable":
        _fire_once(
            state,
            "proxy_unreachable",
            f"*[orkoprox] PROXY UNREACHABLE*\n"
            f"`{PROXY_URL}/v1/healthz` antwortet nicht (Netzwerk/Timeout).\n"
            f"Detail: `{result.detail[:200]}`\n"
            f"ALLE Clients blind.",
        )
        return 1

    if result.kind == "auth_failed":
        _fire_once(
            state,
            "alerter_auth_failed",
            "*[orkoprox] ALERTER AUTH BROKEN*\n"
            "The alerter receives HTTP 401 from the proxy.\n"
            "Proxy itself is alive — alerter API key not in\n"
            "`PROXY_API_KEYS` or was rotated.\n"
            "No client impact. Fix: update `alerter.env` + proxy `.env`.",
        )
        return 1

    if result.kind == "unexpected_status":
        _fire_once(
            state,
            "proxy_unexpected",
            f"*[orkoprox] UNERWARTETE ANTWORT*\n"
            f"Proxy antwortet, aber nicht wie erwartet.\n"
            f"Detail: `{result.detail[:200]}`",
        )
        return 1

    # Proxy-Recovery (alle transient-Alerts clearen falls vorher aktiv)
    _clear_and_recover(
        state,
        "proxy_unreachable",
        f"*[orkoprox] RECOVERY* `{PROXY_URL}` antwortet wieder.",
    )
    _clear_and_recover(state, "proxy_unexpected", "*[orkoprox] RECOVERY* Proxy antwortet wieder normal.")
    _clear_and_recover(state, "alerter_auth_failed", "*[orkoprox] RECOVERY* Alerter-Auth wieder ok.")

    hz = result.data or {"status": "unknown", "backends": []}
    check_backends(hz, state)

    metrics = fetch_metrics()
    check_fallback_rate(metrics, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
