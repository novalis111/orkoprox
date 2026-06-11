"""Tests fuer P3-13 (BUNDLE-A): TTFB-Histogram fuer Streaming-Pfade.

TTFB (Time-to-First-Byte) ist semantisch verschieden von Total-Latency.
TTFB misst Stream-Setup-Latenz (TLS + erste Token-Generation), Total-
Latency misst End-to-End. Eigenes Histogram mit feineren Buckets
(50ms..5s) fuer Grafana-Sichtbarkeit.
"""
from __future__ import annotations


def _parse_metric(body: str, name: str, labels_contains: str = "") -> float | None:
    """Minimaler Prometheus-Parser (Pattern aus test_metrics_extension.py)."""
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line.startswith(name) and labels_contains in line:
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                try:
                    return float(parts[1])
                except ValueError:
                    continue
    return None


def test_ttfb_histogram_record() -> None:
    """record_ttfb() schreibt Sum + Count + Bucket-Counts."""
    from app.metrics import MetricsStore

    m = MetricsStore()
    # Drei TTFB-Werte: 80ms, 300ms, 1500ms
    m.record_ttfb("ovh", "Mistral-Small-3.2-24B-Instruct-2506", 80.0)
    m.record_ttfb("ovh", "Mistral-Small-3.2-24B-Instruct-2506", 300.0)
    m.record_ttfb("ovh", "Mistral-Small-3.2-24B-Instruct-2506", 1500.0)

    body = m.render_prometheus()

    # Count ist 3
    count = _parse_metric(
        body,
        "llm_proxy_ttfb_ms_count",
        labels_contains='provider="ovh"',
    )
    assert count == 3.0

    # Sum ist 80+300+1500 = 1880ms
    s = _parse_metric(
        body,
        "llm_proxy_ttfb_ms_sum",
        labels_contains='provider="ovh"',
    )
    assert s == 1880.0


def test_ttfb_histogram_buckets_cumulative() -> None:
    """Bucket-Counts sind cumulative (Prometheus-Konvention)."""
    from app.metrics import MetricsStore

    m = MetricsStore()
    # Werte: 30ms, 150ms, 800ms
    m.record_ttfb("ovh", "model-x", 30.0)   # ≤50, 100, 250, 500, 1000, 2000, 5000
    m.record_ttfb("ovh", "model-x", 150.0)  # ≤250, 500, 1000, 2000, 5000
    m.record_ttfb("ovh", "model-x", 800.0)  # ≤1000, 2000, 5000

    body = m.render_prometheus()

    # le="50": nur 30ms → 1
    bucket_50 = _parse_metric(body, "llm_proxy_ttfb_ms_bucket", 'le="50.0"')
    assert bucket_50 == 1.0

    # le="100": nur 30ms → 1
    bucket_100 = _parse_metric(body, "llm_proxy_ttfb_ms_bucket", 'le="100.0"')
    assert bucket_100 == 1.0

    # le="250": 30ms + 150ms → 2
    bucket_250 = _parse_metric(body, "llm_proxy_ttfb_ms_bucket", 'le="250.0"')
    assert bucket_250 == 2.0

    # le="1000": alle 3 → 3
    bucket_1000 = _parse_metric(body, "llm_proxy_ttfb_ms_bucket", 'le="1000.0"')
    assert bucket_1000 == 3.0

    # le="+Inf": alle 3 → 3
    bucket_inf = _parse_metric(body, "llm_proxy_ttfb_ms_bucket", 'le="+Inf"')
    assert bucket_inf == 3.0


def test_ttfb_histogram_separate_per_provider_model() -> None:
    """Unterschiedliche (provider, model)-Tupel haben getrennte Histograms."""
    from app.metrics import MetricsStore

    m = MetricsStore()
    m.record_ttfb("ovh", "model-a", 100.0)
    m.record_ttfb("ovh", "model-b", 500.0)

    body = m.render_prometheus()

    count_a = _parse_metric(
        body, "llm_proxy_ttfb_ms_count", 'provider="ovh",model="model-a"'
    )
    count_b = _parse_metric(
        body, "llm_proxy_ttfb_ms_count", 'provider="ovh",model="model-b"'
    )
    assert count_a == 1.0
    assert count_b == 1.0


def test_ttfb_histogram_only_emitted_when_recorded() -> None:
    """Wenn nichts recorded ist, kein TTFB-Block in /metrics."""
    from app.metrics import MetricsStore

    m = MetricsStore()
    body = m.render_prometheus()

    assert "llm_proxy_ttfb_ms" not in body
