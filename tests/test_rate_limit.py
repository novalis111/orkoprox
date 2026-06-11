"""Deterministic tests for RateLimiter — no real sleeps, fake clock only.

The `now` callable parameter lets us advance time without touching wall-clock.
A simple mutable-list clock [t] is the minimal shim: fake_clock() returns
current[0]; advancing time is current[0] += delta.
"""

from __future__ import annotations

from app.rate_limit import RateLimiter


# ── helpers ───────────────────────────────────────────────────────────────────


def make_clock(start: float = 0.0) -> tuple[list[float], "callable[[], float]"]:
    """Return (state, callable). Mutate state[0] to advance time."""
    state = [start]
    return state, lambda: state[0]


# ── disabled rate limiter ─────────────────────────────────────────────────────


def test_rate_disabled_per_minute_zero_always_passes() -> None:
    limiter = RateLimiter(per_minute=0)
    assert limiter.rate_enabled is False
    for _ in range(200):
        assert limiter.check_rate("key") is True


def test_rate_disabled_concurrency_zero_always_passes() -> None:
    limiter = RateLimiter(concurrency=0)
    assert limiter.concurrency_enabled is False
    for _ in range(50):
        assert limiter.try_acquire("key") is True


# ── token-bucket rate limiting ────────────────────────────────────────────────


def test_rate_60rpm_exhausts_after_60_calls() -> None:
    """60 req/min, no burst → first 60 calls pass, 61st is rejected."""
    state, clock = make_clock(0.0)
    limiter = RateLimiter(per_minute=60, burst=0, now=clock)

    # All 60 tokens consumed (bucket starts full = capacity - 1 for first call,
    # then each subsequent call drains one more).
    passed = sum(1 for _ in range(60) if limiter.check_rate("key"))
    assert passed == 60

    # 61st call — bucket empty, time has NOT advanced
    assert limiter.check_rate("key") is False


def test_rate_refills_after_time_advances() -> None:
    """After exhaustion, advancing time by 1 s refills 1 token (60 rpm = 1/s)."""
    state, clock = make_clock(0.0)
    limiter = RateLimiter(per_minute=60, burst=0, now=clock)

    # Drain the bucket
    for _ in range(60):
        limiter.check_rate("key")
    assert limiter.check_rate("key") is False  # confirm exhausted

    # Advance by 1 second → 1 token refilled
    state[0] += 1.0
    assert limiter.check_rate("key") is True
    # Immediately exhausted again
    assert limiter.check_rate("key") is False


def test_burst_allows_initial_spike_above_per_minute() -> None:
    """burst=10, per_minute=6 → first 10 calls pass before rate kicks in."""
    state, clock = make_clock(0.0)
    limiter = RateLimiter(per_minute=6, burst=10, now=clock)

    passed = sum(1 for _ in range(10) if limiter.check_rate("key"))
    assert passed == 10
    # 11th call fails — burst capacity exhausted, no time has passed
    assert limiter.check_rate("key") is False


def test_burst_equal_to_per_minute_behaves_like_no_burst() -> None:
    """burst == per_minute is the default capacity, same behaviour."""
    state, clock = make_clock(0.0)
    limiter = RateLimiter(per_minute=5, burst=5, now=clock)

    passed = sum(1 for _ in range(5) if limiter.check_rate("key"))
    assert passed == 5
    assert limiter.check_rate("key") is False


def test_different_keys_have_independent_buckets() -> None:
    """Rate limit of key-A must not affect key-B."""
    state, clock = make_clock(0.0)
    limiter = RateLimiter(per_minute=2, burst=0, now=clock)

    # Drain key-A
    assert limiter.check_rate("key-A") is True
    assert limiter.check_rate("key-A") is True
    assert limiter.check_rate("key-A") is False  # A exhausted

    # key-B has its own fresh bucket
    assert limiter.check_rate("key-B") is True
    assert limiter.check_rate("key-B") is True
    assert limiter.check_rate("key-B") is False  # B exhausted independently


def test_rate_properties_reflect_config() -> None:
    limiter = RateLimiter(per_minute=10, concurrency=3)
    assert limiter.rate_enabled is True
    assert limiter.concurrency_enabled is True

    limiter_off = RateLimiter(per_minute=0, concurrency=0)
    assert limiter_off.rate_enabled is False
    assert limiter_off.concurrency_enabled is False


# ── concurrency limiter ───────────────────────────────────────────────────────


def test_concurrency_two_slots_third_rejected() -> None:
    """concurrency=2: two in-flight OK, third blocked."""
    limiter = RateLimiter(concurrency=2)

    assert limiter.try_acquire("key") is True
    assert limiter.try_acquire("key") is True
    assert limiter.try_acquire("key") is False  # at capacity


def test_concurrency_release_frees_slot() -> None:
    """After release(), a new request can acquire a slot."""
    limiter = RateLimiter(concurrency=1)

    assert limiter.try_acquire("key") is True
    assert limiter.try_acquire("key") is False  # full

    limiter.release("key")
    assert limiter.try_acquire("key") is True  # slot freed


def test_concurrency_different_keys_independent() -> None:
    """Slots are per-key, not global."""
    limiter = RateLimiter(concurrency=1)

    assert limiter.try_acquire("key-A") is True
    assert limiter.try_acquire("key-A") is False  # A full

    # key-B has its own slot — unaffected
    assert limiter.try_acquire("key-B") is True
    assert limiter.try_acquire("key-B") is False  # B full independently


def test_release_without_acquire_does_not_raise() -> None:
    """Spurious release must not crash (counter must not go below zero)."""
    limiter = RateLimiter(concurrency=2)
    limiter.release("never-acquired")  # should not raise


def test_concurrency_disabled_never_blocks() -> None:
    limiter = RateLimiter(concurrency=0)
    for _ in range(100):
        assert limiter.try_acquire("key") is True
