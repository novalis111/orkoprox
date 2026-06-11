from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from threading import Lock


class MetricsStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self._latency_ms_sum: dict[tuple[str, str, str, str], float] = defaultdict(float)
        # Per-provider daily token usage: key = (provider, date_str)
        self._provider_token_usage: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "request_count": 0,
            }
        )
        # Per-key request counter: key = api_key (or "anonymous")
        self._per_key_request_count: dict[str, int] = defaultdict(int)
        # Backend-Health: 0=down, 1=up (gesetzt durch /v1/healthz)
        self._backend_up: dict[str, int] = {}
        # Latency-Histogram: Buckets in ms, pro (provider, model, status, stream).
        # Buckets an SLO orientiert: 100ms, 500ms, 1s, 2s, 5s, 10s, 30s, 60s, +Inf.
        self._latency_buckets_ms: tuple[float, ...] = (
            100.0,
            500.0,
            1000.0,
            2000.0,
            5000.0,
            10000.0,
            30000.0,
            60000.0,
        )
        self._latency_bucket_counts: dict[tuple[str, str, str, str, float], int] = defaultdict(int)
        self._latency_count: dict[tuple[str, str, str, str], int] = defaultdict(int)
        # Fallback-Chain-Hits: (from_provider, to_provider) -> count
        self._fallback_hits: dict[tuple[str, str], int] = defaultdict(int)
        # TTFB-Histogram (P3-13, BUNDLE-A): nur Streaming-Pfade. Profil
        # unterscheidet sich von Total-Latency (TTFB ist meistens 200-2000ms,
        # Total-Latency 1-30s je nach Output-Laenge).
        # Buckets an Streaming-SLO orientiert: 50ms..5s.
        self._ttfb_buckets_ms: tuple[float, ...] = (
            50.0,
            100.0,
            250.0,
            500.0,
            1000.0,
            2000.0,
            5000.0,
        )
        self._ttfb_bucket_counts: dict[tuple[str, str, float], int] = defaultdict(int)
        self._ttfb_count: dict[tuple[str, str], int] = defaultdict(int)
        self._ttfb_sum_ms: dict[tuple[str, str], float] = defaultdict(float)

    def record(
        self, provider: str, model: str, status_code: int, stream: bool, latency_ms: float
    ) -> None:
        key = (provider, model, str(status_code), str(stream).lower())
        with self._lock:
            self._counts[key] += 1
            self._latency_ms_sum[key] += latency_ms
            self._latency_count[key] += 1
            for bucket in self._latency_buckets_ms:
                if latency_ms <= bucket:
                    self._latency_bucket_counts[(*key, bucket)] += 1

    def set_backend_up(self, backend: str, up: int) -> None:
        """Gauge fuer /metrics: llm_proxy_backend_up{backend="..."}."""
        with self._lock:
            self._backend_up[backend] = 1 if up else 0

    def record_fallback_hit(self, from_provider: str, to_provider: str) -> None:
        """Wenn ein Fallback in der Provider-Kette greift."""
        with self._lock:
            self._fallback_hits[(from_provider, to_provider)] += 1

    def record_ttfb(self, provider: str, model: str, ttfb_ms: float) -> None:
        """Time-to-First-Byte fuer Streaming-Pfade (P3-13, BUNDLE-A).

        Aufruf NUR beim allerersten Chunk eines Streams. TTFB-Profil
        unterscheidet sich strukturell von Total-Latency — daher eigenes
        Histogram mit feineren Sub-Sekunden-Buckets.
        """
        key = (provider, model)
        with self._lock:
            self._ttfb_count[key] += 1
            self._ttfb_sum_ms[key] += ttfb_ms
            for bucket in self._ttfb_buckets_ms:
                if ttfb_ms <= bucket:
                    self._ttfb_bucket_counts[(*key, bucket)] += 1

    def record_provider_token_usage(
        self,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Track token usage per provider per day for quota monitoring."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = (provider, today)
        total = prompt_tokens + completion_tokens
        with self._lock:
            bucket = self._provider_token_usage[key]
            bucket["prompt_tokens"] += prompt_tokens
            bucket["completion_tokens"] += completion_tokens
            bucket["total_tokens"] += total
            bucket["request_count"] += 1

    def record_per_key_request(self, api_key: str | None) -> None:
        """Increment request counter for an API key."""
        normalized = api_key or "anonymous"
        with self._lock:
            self._per_key_request_count[normalized] += 1

    def get_provider_token_usage(self, date: str | None = None) -> dict[str, dict[str, int]]:
        """Return per-provider token usage for a given date (default: today)."""
        if date is None:
            date = datetime.now(UTC).strftime("%Y-%m-%d")
        result: dict[str, dict[str, int]] = {}
        with self._lock:
            for (provider, day), usage in self._provider_token_usage.items():
                if day == date:
                    result[provider] = dict(usage)
        return result

    def get_per_key_request_counts(self) -> dict[str, int]:
        """Return request counts per API key."""
        with self._lock:
            return dict(self._per_key_request_count)

    def render_prometheus(self) -> str:
        lines = [
            "# HELP llm_proxy_requests_total Total number of requests",
            "# TYPE llm_proxy_requests_total counter",
            "# HELP llm_proxy_request_latency_ms_sum Sum of request latencies in milliseconds",
            "# TYPE llm_proxy_request_latency_ms_sum counter",
        ]
        with self._lock:
            for (provider, model, status_code, stream), value in sorted(self._counts.items()):
                labels = f'provider="{provider}",model="{model}",status_code="{status_code}",stream="{stream}"'
                lines.append(f"llm_proxy_requests_total{{{labels}}} {value}")
                lines.append(
                    f"llm_proxy_request_latency_ms_sum{{{labels}}} {self._latency_ms_sum[(provider, model, status_code, stream)]:.2f}"
                )
            # Per-provider daily token usage
            if self._provider_token_usage:
                lines.append(
                    "# HELP llm_proxy_provider_tokens_total Daily token usage per provider"
                )
                lines.append("# TYPE llm_proxy_provider_tokens_total counter")
                for (provider, date), usage in sorted(self._provider_token_usage.items()):
                    labels = f'provider="{provider}",date="{date}"'
                    lines.append(
                        f"llm_proxy_provider_tokens_total{{{labels}}} {usage['total_tokens']}"
                    )
            # Per-key request counts
            if self._per_key_request_count:
                lines.append("# HELP llm_proxy_per_key_requests_total Requests per API key")
                lines.append("# TYPE llm_proxy_per_key_requests_total counter")
                for key, count in sorted(self._per_key_request_count.items()):
                    # Mask key for safety: show first 8 chars
                    safe_key = key[:8] + "..." if len(key) > 12 else key
                    lines.append(
                        f'llm_proxy_per_key_requests_total{{api_key="{safe_key}"}} {count}'
                    )
            # Backend-Up-Gauge (0=down, 1=up). Geschrieben von /v1/healthz.
            if self._backend_up:
                lines.append("# HELP llm_proxy_backend_up Backend reachability (0=down, 1=up)")
                lines.append("# TYPE llm_proxy_backend_up gauge")
                for backend, up in sorted(self._backend_up.items()):
                    lines.append(f'llm_proxy_backend_up{{backend="{backend}"}} {up}')
            # Latency-Histogram pro (provider, model, status_code, stream).
            if self._latency_count:
                lines.append(
                    "# HELP llm_proxy_request_latency_ms Request latency histogram in milliseconds"
                )
                lines.append("# TYPE llm_proxy_request_latency_ms histogram")
                seen_keys: set[tuple[str, str, str, str]] = set()
                for key in sorted(self._latency_count.keys()):
                    provider, model, status_code, stream = key
                    seen_keys.add(key)
                    base_labels = (
                        f'provider="{provider}",model="{model}",'
                        f'status_code="{status_code}",stream="{stream}"'
                    )
                    for bucket in self._latency_buckets_ms:
                        count = self._latency_bucket_counts.get((*key, bucket), 0)
                        lines.append(
                            f'llm_proxy_request_latency_ms_bucket{{{base_labels},le="{bucket}"}} {count}'
                        )
                    total_count = self._latency_count[key]
                    lines.append(
                        f'llm_proxy_request_latency_ms_bucket{{{base_labels},le="+Inf"}} {total_count}'
                    )
                    lines.append(
                        f'llm_proxy_request_latency_ms_count{{{base_labels}}} {total_count}'
                    )
            # Fallback-Chain-Hits
            if self._fallback_hits:
                lines.append(
                    "# HELP llm_proxy_fallback_hits_total Number of fallback-chain activations"
                )
                lines.append("# TYPE llm_proxy_fallback_hits_total counter")
                for (from_p, to_p), value in sorted(self._fallback_hits.items()):
                    lines.append(
                        f'llm_proxy_fallback_hits_total{{from_provider="{from_p}",'
                        f'to_provider="{to_p}"}} {value}'
                    )
            # TTFB-Histogram fuer Streaming-Pfade (P3-13, BUNDLE-A).
            if self._ttfb_count:
                lines.append(
                    "# HELP llm_proxy_ttfb_ms Time-to-first-byte histogram in milliseconds (streaming only)"
                )
                lines.append("# TYPE llm_proxy_ttfb_ms histogram")
                for key in sorted(self._ttfb_count.keys()):
                    provider, model = key
                    base_labels = f'provider="{provider}",model="{model}"'
                    for bucket in self._ttfb_buckets_ms:
                        count = self._ttfb_bucket_counts.get((*key, bucket), 0)
                        lines.append(
                            f'llm_proxy_ttfb_ms_bucket{{{base_labels},le="{bucket}"}} {count}'
                        )
                    total_count = self._ttfb_count[key]
                    lines.append(
                        f'llm_proxy_ttfb_ms_bucket{{{base_labels},le="+Inf"}} {total_count}'
                    )
                    lines.append(
                        f'llm_proxy_ttfb_ms_count{{{base_labels}}} {total_count}'
                    )
                    lines.append(
                        f'llm_proxy_ttfb_ms_sum{{{base_labels}}} {self._ttfb_sum_ms[key]:.2f}'
                    )
        return "\n".join(lines) + "\n"
