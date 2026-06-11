"""Tests fuer neue Metriken: backend_up, latency_histogram, fallback_hits."""

from __future__ import annotations


def _parse_metric(body: str, name: str, labels_contains: str = "") -> float | None:
    """Minimaler Prometheus-Parser: finde erste Zeile die mit name startet und
    labels_contains enthaelt, gib den Value zurueck."""
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


def test_set_backend_up_wird_exponiert():
    from app.metrics import MetricsStore

    m = MetricsStore()
    m.set_backend_up("ovh", 1)
    m.set_backend_up("ollama", 0)
    m.set_backend_up("whisper_sidecar", 1)

    body = m.render_prometheus()
    assert 'llm_proxy_backend_up{backend="ovh"} 1' in body
    assert 'llm_proxy_backend_up{backend="ollama"} 0' in body
    assert 'llm_proxy_backend_up{backend="whisper_sidecar"} 1' in body
    assert "# TYPE llm_proxy_backend_up gauge" in body


def test_latency_histogram_buckets_werden_geschrieben():
    from app.metrics import MetricsStore

    m = MetricsStore()
    # 3 Requests: 50ms, 250ms, 7000ms
    m.record("ovh", "low", 200, False, 50.0)
    m.record("ovh", "low", 200, False, 250.0)
    m.record("ovh", "low", 200, False, 7000.0)

    body = m.render_prometheus()
    # Alle 3 Requests sind <= 10000ms
    val_10s = _parse_metric(
        body,
        "llm_proxy_request_latency_ms_bucket",
        'provider="ovh",model="low",status_code="200",stream="false",le="10000.0"',
    )
    assert val_10s == 3.0

    # Nur 2 Requests sind <= 500ms (50, 250)
    val_500 = _parse_metric(
        body,
        "llm_proxy_request_latency_ms_bucket",
        'provider="ovh",model="low",status_code="200",stream="false",le="500.0"',
    )
    assert val_500 == 2.0

    # Count-Metric
    count = _parse_metric(
        body,
        "llm_proxy_request_latency_ms_count",
        'provider="ovh",model="low",status_code="200",stream="false"',
    )
    assert count == 3.0

    # +Inf bucket hat alle 3
    val_inf = _parse_metric(
        body,
        "llm_proxy_request_latency_ms_bucket",
        'provider="ovh",model="low",status_code="200",stream="false",le="+Inf"',
    )
    assert val_inf == 3.0


def test_fallback_hits_werden_gezaehlt():
    from app.metrics import MetricsStore

    m = MetricsStore()
    m.record_fallback_hit("ovh", "ollama")
    m.record_fallback_hit("ovh", "ollama")
    m.record_fallback_hit("ovh", "stub")

    body = m.render_prometheus()
    assert (
        'llm_proxy_fallback_hits_total{from_provider="ovh",to_provider="ollama"} 2'
        in body
    )
    assert (
        'llm_proxy_fallback_hits_total{from_provider="ovh",to_provider="stub"} 1'
        in body
    )


def test_record_fallback_if_needed_zaehlt_nur_bei_wechsel():
    from app.main import _record_fallback_if_needed
    from app.metrics import MetricsStore

    # Neu initialisieren, damit wir sauber zaehlen.
    import app.main as main_module

    original = main_module.metrics_store
    test_store = MetricsStore()
    main_module.metrics_store = test_store
    try:
        # Kein Wechsel -> kein Hit
        _record_fallback_if_needed(
            "ovh",
            {"resolved_provider": "ovh", "fallback_chain": ["ovh", "ollama"]},
        )
        body = test_store.render_prometheus()
        assert "llm_proxy_fallback_hits_total" not in body

        # Wechsel -> Hit
        _record_fallback_if_needed(
            "ollama",
            {"resolved_provider": "ovh", "fallback_chain": ["ovh", "ollama"]},
        )
        body = test_store.render_prometheus()
        assert (
            'llm_proxy_fallback_hits_total{from_provider="ovh",to_provider="ollama"} 1'
            in body
        )

        # route_debug=None -> kein Crash, kein Zaehlen
        _record_fallback_if_needed("ollama", None)
        _record_fallback_if_needed("ollama", {"no_resolved_provider": True})
    finally:
        main_module.metrics_store = original
