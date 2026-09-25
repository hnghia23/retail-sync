"""Bảo trì định kỳ của trung tâm (`central.ops.maintenance`) trên Postgres THẬT.

Ràng buộc #2: hết partition `point_ledger` = mọi sự kiện có điểm bị từ chối. Test dựng đúng tình
huống "lịch không chạy mấy tháng" (xóa các partition tương lai) rồi đòi một lượt bảo trì trả vùng
đệm về đủ — và đòi hai việc của lượt đó độc lập: partition lỗi không được kéo đối soát chết theo.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from central.ops import maintenance
from shared.config import ReturnRules
from shared.db import make_engine, make_session_factory


@pytest.fixture
async def factory(central_db: Any, postgres_dsn: str) -> AsyncIterator[Any]:
    dbname = await central_db.fetchval("SELECT current_database()")
    base = postgres_dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
    engine = make_engine(f"{base}/{dbname}")
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


async def _future_partitions(db: Any) -> list[str]:
    rows = await db.fetch(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
        " WHERE i.inhparent = 'point_ledger'::regclass"
        " AND c.relname > 'point_ledger_' || to_char(now(), 'YYYY_MM') ORDER BY 1"
    )
    return [r["relname"] for r in rows]


async def test_one_run_restores_the_partition_buffer(central_db: Any, factory: Any) -> None:
    ahead = await _future_partitions(central_db)
    assert len(ahead) == 3  # migration tạo sẵn 3 tháng tới
    for name in ahead:  # "lịch không chạy 3 tháng": vùng đệm cạn
        await central_db.execute(f"DROP TABLE {name}")
    empty = await central_db.fetchrow("SELECT * FROM point_ledger_partition_health")
    assert empty["months_ahead"] == 0

    result = await maintenance.run_once(factory, rules=ReturnRules(_env_file=None), months_ahead=3)

    assert result.errors == {}
    assert result.partitions is not None and result.partitions.is_healthy
    assert result.partitions.months_ahead == 3
    assert await _future_partitions(central_db) == ahead
    assert result.reconcile is not None and result.reconcile.drifts == []
    assert "đệm 3 tháng" in maintenance.describe(result)


async def test_partition_failure_does_not_stop_reconciliation(
    central_db: Any, factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: Any, **__: Any) -> Any:
        raise PermissionError("must be owner of table point_ledger")

    monkeypatch.setattr(maintenance, "run_daily_maintenance", broken)
    result = await maintenance.run_once(factory, rules=ReturnRules(_env_file=None), months_ahead=3)

    assert "partitions" in result.errors
    assert result.partitions is None
    assert result.reconcile is not None  # đối soát vẫn chạy
    assert await central_db.fetchval(
        "SELECT last_run_at IS NOT NULL FROM reconciliation_watermark"
        " WHERE job_name = 'point_balance_vs_ledger'"
    )


async def test_full_scan_runs_when_due_and_records_its_own_watermark(
    central_db: Any, factory: Any
) -> None:
    """Lượt tăng dần đầu tiên chưa có mốc cũng quét hết; sau đó chỉ tới hạn mới quét toàn bộ, và
    mốc riêng `point_balance_vs_ledger:full` là thứ bộ giám sát canh (rs-reconcile-full)."""
    from datetime import UTC, datetime

    policy = maintenance.FullScanPolicy(every_days=30, start_hour=1, end_hour=5)
    off_peak = datetime(2026, 9, 24, 19, 0, tzinfo=UTC)  # 02:00 giờ cửa hàng
    rules = ReturnRules(_env_file=None)

    first = await maintenance.run_once(
        factory, rules=rules, months_ahead=3, full_scan=policy, now=off_peak
    )
    assert first.reconcile is not None and first.reconcile.full
    full_at = await central_db.fetchval(
        "SELECT last_run_at FROM reconciliation_watermark"
        " WHERE job_name = 'point_balance_vs_ledger:full'"
    )
    assert full_at is not None

    again = await maintenance.run_once(
        factory, rules=rules, months_ahead=3, full_scan=policy, now=off_peak
    )
    assert again.reconcile is not None and not again.reconcile.full  # chưa tới hạn: tăng dần
    assert "reconcile[incremental]" in maintenance.describe(again)
