"""Đối soát INV-4 — `central.ops.reconcile`. AT-10, DI-1, ràng buộc #8 (tăng dần).

Dữ liệu dựng bằng SQL để điều khiển chính xác từng lệch. Test "ingest thật → 0 lệch" nằm ở
`test_sync_pipeline.py`, nơi điểm đi qua đúng đường ghi production.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from central.ops.reconcile import reconcile_points
from shared.config import ReturnRules

RULES = ReturnRules(_env_file=None)
A = uuid.UUID("01935abc-0000-7000-8000-00000000a001")
B = uuid.UUID("01935abc-0000-7000-8000-00000000b001")


@pytest.fixture
async def factory(central_db: Any, postgres_dsn: str) -> AsyncIterator[Any]:
    from shared.db import make_engine, make_session_factory

    dbname = await central_db.fetchval("SELECT current_database()")
    base = postgres_dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
    engine = make_engine(f"{base}/{dbname}")
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


async def _customer(db: Any, customer_id: uuid.UUID) -> None:
    await db.execute(
        "INSERT INTO customer (customer_id, phone_hash) VALUES ($1, $2)",
        customer_id,
        str(customer_id),
    )


async def _earn(db: Any, customer_id: uuid.UUID, delta: int, reason: str = "EARN") -> None:
    """Ghi ledger + cộng snapshot như handler ingest làm — dữ liệu KHỚP."""
    await db.execute(
        """INSERT INTO point_ledger (event_id, customer_id, store_id, delta, reason, occurred_at)
           VALUES ($1, $2, 'store-001', $3, $4, now())""",
        uuid.uuid4(),
        customer_id,
        delta,
        reason,
    )
    earned = delta if reason == "EARN" and delta > 0 else 0
    await db.execute(
        """INSERT INTO point_balance (customer_id, balance, lifetime_earned)
           VALUES ($1, $2, $3)
           ON CONFLICT (customer_id) DO UPDATE SET
             balance = point_balance.balance + EXCLUDED.balance,
             lifetime_earned = point_balance.lifetime_earned + EXCLUDED.lifetime_earned""",
        customer_id,
        delta,
        earned,
    )


async def _run(factory: Any, **kw: Any) -> Any:
    # safety_lag 0: dữ liệu test vừa ghi xong, không có transaction nào đang mở.
    return await reconcile_points(factory, rules=RULES, safety_lag_seconds=0, **kw)


async def test_consistent_ledger_reports_no_drift_and_advances_watermark(
    central_db: Any, factory: Any
) -> None:
    await _customer(central_db, A)
    await _earn(central_db, A, 50)
    await _earn(central_db, A, -20, "RETURN")

    report = await _run(factory)
    assert report.checked_customers == 1
    assert report.drifts == []
    mark = await central_db.fetchrow("SELECT * FROM reconciliation_watermark")
    assert mark["last_event_at"] == report.window_end and mark["mismatch_count"] == 0


async def test_tampered_snapshot_is_recorded_then_resolved_after_repair(
    central_db: Any, factory: Any
) -> None:
    """Lệch không chỉ nằm trong log: nó nằm trong bảng tới khi có người sửa, và lần chạy sau
    xác nhận bản sửa — kể cả khi khách không phát sinh giao dịch nào nữa."""
    await _customer(central_db, A)
    await _earn(central_db, A, 50)
    await central_db.execute("UPDATE point_balance SET balance = 49 WHERE customer_id = $1", A)

    first = await _run(factory)
    assert [(d.field, d.expected, d.actual) for d in first.drifts] == [("balance", "50", "49")]
    assert (
        await central_db.fetchval(
            "SELECT count(*) FROM reconciliation_drift WHERE resolved_at IS NULL"
        )
        == 1
    )

    await central_db.execute("UPDATE point_balance SET balance = 50 WHERE customer_id = $1", A)
    second = await _run(factory)  # không có dòng ledger mới — khách vẫn được xét lại
    assert second.checked_customers == 1 and second.drifts == []
    assert (
        await central_db.fetchval(
            "SELECT count(*) FROM reconciliation_drift WHERE resolved_at IS NOT NULL"
        )
        == 1
    )


async def test_lifetime_and_tier_drift_are_caught(central_db: Any, factory: Any) -> None:
    """Ràng buộc #6: hạng suy từ `lifetime_earned`. Snapshot sai hạng = giảm giá sai %."""
    await central_db.execute(
        "INSERT INTO tier_rule (tier, min_lifetime_points, discount_pct)"
        " VALUES ('BRONZE', 0, 0), ('SILVER', 1000, 3)"
    )
    await _customer(central_db, A)
    await _earn(central_db, A, 1200)
    await central_db.execute("UPDATE point_balance SET tier = 'BRONZE' WHERE customer_id = $1", A)
    await _customer(central_db, B)
    await _earn(central_db, B, 30)
    await central_db.execute(
        "UPDATE point_balance SET lifetime_earned = 0 WHERE customer_id = $1", B
    )

    drifts = {(d.customer_id, d.field) for d in (await _run(factory)).drifts}
    # B: lifetime 30 vẫn là BRONZE nên hạng không lệch, chỉ lifetime lệch.
    assert drifts == {(A, "tier"), (B, "lifetime_earned")}


async def test_ledger_without_snapshot_is_drift(central_db: Any, factory: Any) -> None:
    await _customer(central_db, A)
    await central_db.execute(
        """INSERT INTO point_ledger (event_id, customer_id, store_id, delta, reason, occurred_at)
           VALUES ($1, $2, 'store-001', 10, 'EARN', now())""",
        uuid.uuid4(),
        A,
    )
    [drift] = (await _run(factory)).drifts
    assert (drift.field, drift.actual) == ("missing", None)


async def test_incremental_misses_untouched_customer_but_full_scan_finds_it(
    central_db: Any, factory: Any
) -> None:
    """Đánh đổi có chủ đích của ràng buộc #8: chạy tăng dần chỉ xét khách có biến động mới.

    Snapshot của khách A bị hỏng SAU lần đối soát, và A không mua gì thêm → lần tăng dần
    không thấy. Lần `--full` hằng tháng thấy. Nếu một ngày test này đỏ ở vế đầu, nghĩa là
    job đã lặng lẽ thành quét toàn bộ — đọc lại docs/08 §4.2 trước khi chấp nhận.
    """
    await _customer(central_db, A)
    await _earn(central_db, A, 50)
    await _customer(central_db, B)
    await _earn(central_db, B, 10)
    assert (await _run(factory)).drifts == []

    await central_db.execute("UPDATE point_balance SET balance = 0 WHERE customer_id = $1", A)
    await _earn(central_db, B, 5)

    incremental = await _run(factory)
    assert incremental.checked_customers == 1  # chỉ B
    assert incremental.drifts == []

    full = await _run(factory, full=True)
    assert full.checked_customers == 2
    assert [(d.customer_id, d.field) for d in full.drifts] == [(A, "balance")]
