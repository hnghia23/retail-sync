"""Central API nhiều tiến trình (ADR-011): mỗi tiến trình lấy đúng phần ngân sách của nó."""

from __future__ import annotations

from central.settings import CentralSettings, worker_budget
from shared.db import MAX_OVERFLOW, POOL_SIZE


def _settings(**kw: int) -> CentralSettings:
    return CentralSettings(_env_file=None).model_copy(update=kw)


def test_one_worker_keeps_the_pre_adr011_budget() -> None:
    s = _settings()
    b = worker_budget(s)
    assert b.ingest_concurrency == s.ingest_max_concurrency
    assert (b.pool_size, b.max_overflow) == (POOL_SIZE, MAX_OVERFLOW)
    assert (b.store_rate_per_second, b.store_burst) == (
        s.ingest_store_rate_per_second,
        s.ingest_store_burst,
    )


def test_postgres_budget_is_split_but_never_multiplied() -> None:
    """Semaphore + pool bảo vệ Postgres: N tiến trình cộng lại xấp xỉ ngân sách một tiến trình,
    không phải N lần (`max_connections` là trần chung — LD-4)."""
    s = _settings(central_api_workers=4, ingest_max_concurrency=8)
    b = worker_budget(s)
    assert b.ingest_concurrency == 2
    assert 4 * (b.pool_size + b.max_overflow) <= 2 * (POOL_SIZE + MAX_OVERFLOW) + 8
    assert b.pool_size + b.max_overflow > b.ingest_concurrency  # còn chỗ cho /health


def test_per_store_rate_limit_is_not_divided() -> None:
    """Keep-alive: mọi lô của MỘT cửa hàng thường vào cùng một tiến trình. Chia rate cho N thì
    cửa hàng chỉ còn 1/N hạn mức."""
    s = _settings(central_api_workers=4)
    assert worker_budget(s).store_rate_per_second == s.ingest_store_rate_per_second


def test_more_workers_than_budget_still_leaves_one_slot_each() -> None:
    b = worker_budget(_settings(central_api_workers=16, ingest_max_concurrency=8))
    assert b.ingest_concurrency == 1 and b.pool_size >= 2
