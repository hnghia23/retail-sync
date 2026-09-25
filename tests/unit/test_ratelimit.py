"""Token bucket theo cửa hàng của `POST /events` (`central.ingest.ratelimit`, docs/08 §3.2)."""

from __future__ import annotations

from central.ingest.ratelimit import StoreRateLimiter


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_burst_then_refill_at_rate() -> None:
    clock = Clock()
    limiter = StoreRateLimiter(rate=10, burst=3, clock=clock)
    assert [limiter.acquire("a") for _ in range(3)] == [None, None, None]
    wait = limiter.acquire("a")
    assert wait is not None and abs(wait - 0.1) < 1e-9  # 1 lượt mỗi 1/10 giây
    clock.t = 0.1
    assert limiter.acquire("a") is None
    clock.t = 100  # nghỉ lâu không dồn quá `burst`
    assert [limiter.acquire("a") for _ in range(4)][-1] is not None


def test_each_store_has_its_own_bucket() -> None:
    limiter = StoreRateLimiter(rate=1, burst=1, clock=Clock())
    assert limiter.acquire("a") is None
    assert limiter.acquire("a") is not None
    assert limiter.acquire("b") is None  # cửa hàng khác không bị vạ lây


def test_zero_rate_disables_the_limit() -> None:
    limiter = StoreRateLimiter(rate=0, burst=1, clock=Clock())
    assert all(limiter.acquire("a") is None for _ in range(1000))


def test_retry_after_is_whole_seconds_rounded_up() -> None:
    assert StoreRateLimiter.retry_after_header(0.01) == "1"
    assert StoreRateLimiter.retry_after_header(1.2) == "2"
