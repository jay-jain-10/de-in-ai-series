from src.circuit_breaker import CLOSED, HALF_OPEN, OPEN, BreakerRegistry, CircuitBreaker
from src.config import BreakerSettings
from tests.conftest import FakeClock


def make(clock=None, rng=lambda: 0.5):
    clock = clock or FakeClock()
    return CircuitBreaker("haiku", BreakerSettings(), clock=clock, rng=rng), clock


def test_opens_after_5_consecutive_failures():
    b, _ = make()
    for _ in range(4):
        b.record_failure()
    assert b.state == CLOSED and b.allow_request()
    b.record_failure()
    assert b.state == OPEN and not b.allow_request()


def test_success_resets_consecutive_count():
    b, _ = make()
    for _ in range(4):
        b.record_failure()
    b.record_success()
    for _ in range(4):
        b.record_failure()
    assert b.state == CLOSED


def test_stays_open_until_60s():
    b, clock = make()
    for _ in range(5):
        b.record_failure()
    clock.advance(59)
    assert not b.allow_request()
    clock.advance(1)
    assert b.allow_request() and b.state == HALF_OPEN


def test_half_open_samples_one_percent_then_ramps_to_closed():
    b, clock = make(rng=lambda: 0.5)  # rng 0.5 never under 1%
    for _ in range(5):
        b.record_failure()
    clock.advance(60)
    assert b.allow_request()           # guaranteed probe after 60s
    assert not b.allow_request()       # 1% sampling rejects the rest
    b.record_success()                 # ramp 1% -> 10%
    assert not b.allow_request()
    b._rng = lambda: 0.05              # under 10%
    assert b.allow_request()
    b.record_success()                 # -> 50%
    b._rng = lambda: 0.4
    assert b.allow_request()
    b.record_success()                 # -> 100%
    b._rng = lambda: 0.99
    assert b.state == HALF_OPEN and b.allow_request()
    b.record_success()
    assert b.state == CLOSED


def test_failed_probe_reopens():
    b, clock = make()
    for _ in range(5):
        b.record_failure()
    clock.advance(60)
    assert b.allow_request()
    b.record_failure()
    assert b.state == OPEN
    clock.advance(30)
    assert not b.allow_request()


def test_probe_every_60s_even_if_unlucky():
    b, clock = make(rng=lambda: 0.99)
    for _ in range(5):
        b.record_failure()
    clock.advance(60)
    assert b.allow_request()
    assert not b.allow_request()
    clock.advance(60)
    assert b.allow_request()


def test_fallback_rate_and_alert_threshold():
    reg = BreakerRegistry(BreakerSettings())
    b = reg.get("haiku")
    for _ in range(100):
        b.allow_request()
    for _ in range(5):
        b.record_fallback()
    assert reg.alerts() == []          # exactly 5% is not above threshold
    b.record_fallback()
    assert reg.alerts() == ["haiku"]


def test_health_snapshot():
    b, clock = make()
    b.record_success()
    clock.advance(10)
    h = b.health()
    assert h["state"] == CLOSED and h["seconds_since_last_success"] == 10
    assert h["consecutive_failures"] == 0
