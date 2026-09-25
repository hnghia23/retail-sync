"""Schema trung tâm — trọng tâm là partition `point_ledger` và chốt chặn idempotency.

`test_partition_autocreate.py` ở docs/16 §1 được gộp vào đây: nó kiểm cùng một cơ chế,
và tách file chỉ làm hai chỗ cùng dựng container.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

CUSTOMER_ID = uuid.UUID("01935abc-0000-7000-8000-000000000001")


async def _seed(db: Any) -> None:
    await db.execute("INSERT INTO region (region_id, name) VALUES ('north', 'Mien Bac')")
    await db.execute(
        "INSERT INTO store (store_id, name, region_id) VALUES ('store-001', 'CH 001', 'north')"
    )
    await db.execute(
        "INSERT INTO customer (customer_id, phone_hash) VALUES ($1, 'h1')", CUSTOMER_ID
    )


async def _insert_ledger(
    db: Any, *, months_offset: int = 0, event_id: uuid.UUID | None = None
) -> None:
    await db.execute(
        """
        INSERT INTO point_ledger (event_id, customer_id, store_id, delta, reason, occurred_at)
        VALUES ($1, $2, 'store-001', 53, 'EARN', now() + make_interval(months => $3))
        """,
        event_id or uuid.uuid4(),
        CUSTOMER_ID,
        months_offset,
    )


# ═══════════════ Ràng buộc #2 — rủi ro sập cao nhất của thiết kế ═══════════════


async def test_partitions_exist_three_months_ahead(central_db: Any) -> None:
    """Migration phải tạo sẵn tháng trước + tháng hiện tại + 3 tháng tới.

    Postgres không tự tạo partition tương lai. Hết partition = MỌI insert điểm lỗi, và nó
    xảy ra đúng 00:00 ngày đầu tháng, lúc không ai trực (docs/08 §4.1).
    """
    await _seed(central_db)
    health = await central_db.fetchrow("SELECT * FROM point_ledger_partition_health")
    assert health["partition_count"] == 5
    assert health["months_ahead"] == 3


async def test_last_month_is_accepted_on_a_fresh_database(central_db: Any) -> None:
    """Cửa hàng offline vắt qua cuối tháng, đồng bộ ngày 1: điểm của tháng trước phải vào được
    NGAY ở tháng go-live (migration 0006, phát hiện bằng bộ giả lập `virtual`). Lùi hai tháng
    thì vẫn từ chối ồn ào — đồng hồ sai hoặc dữ liệu quá cũ."""
    import asyncpg

    await _seed(central_db)
    await _insert_ledger(central_db, months_offset=-1)
    assert await central_db.fetchval("SELECT count(*) FROM point_ledger") == 1
    with pytest.raises(asyncpg.CheckViolationError, match="no partition"):
        await _insert_ledger(central_db, months_offset=-2)


async def test_ledger_row_lands_in_month_partition(central_db: Any) -> None:
    await _seed(central_db)
    await _insert_ledger(central_db)
    partition = await central_db.fetchval("SELECT tableoid::regclass::text FROM point_ledger")
    assert partition.startswith("point_ledger_")


async def test_insert_beyond_partitions_fails_loudly(central_db: Any) -> None:
    """Không có DEFAULT partition — có chủ đích.

    DEFAULT partition biến lỗi ồn ào (insert fail, thấy ngay) thành lỗi im lặng (dữ liệu
    rơi vào default), rồi sau đó chặn luôn việc tạo partition đúng tháng vì default đã
    chứa dòng thuộc khoảng đó. Thà hỏng to và sớm.
    """
    import asyncpg

    await _seed(central_db)
    # SQLSTATE 23514 — Postgres báo lỗi này là check_violation, không phải undefined_table.
    with pytest.raises(asyncpg.CheckViolationError, match="no partition"):
        await _insert_ledger(central_db, months_offset=12)


async def test_ensure_partitions_is_idempotent_and_unblocks_insert(central_db: Any) -> None:
    """Chạy job hai lần không được tạo trùng — job này sẽ chạy HẰNG NGÀY."""
    await _seed(central_db)

    created_first = await central_db.fetchval("SELECT ensure_point_ledger_partitions(12)")
    created_second = await central_db.fetchval("SELECT ensure_point_ledger_partitions(12)")
    assert created_first > 0
    assert created_second == 0, "chạy lần hai đã tạo trùng partition"

    await _insert_ledger(central_db, months_offset=12)
    assert await central_db.fetchval("SELECT count(*) FROM point_ledger") == 1


async def test_ensure_partitions_rejects_zero_months(central_db: Any) -> None:
    """`months_ahead = 0` nghĩa là không có vùng đệm nào — chặn ở chính hàm."""
    import asyncpg

    with pytest.raises(asyncpg.RaiseError):
        await central_db.fetchval("SELECT ensure_point_ledger_partitions(0)")


# ═══════════════ docs/12 §5 — chốt chặn idempotency ═══════════════


async def test_duplicate_event_is_claimed_once(central_db: Any) -> None:
    """AT-03: gửi lặp một sự kiện 5 lần → điểm chỉ cộng một lần.

    Đây là test ở mức SQL cho chính cơ chế `central.ingest.idempotency.claim_event()`:
    chỉ lần INSERT đầu tiên có `rowcount = 1`.
    """
    event_id = uuid.uuid4()
    claimed = 0
    for _ in range(5):
        result = await central_db.execute(
            """INSERT INTO processed_event (event_id, received_at) VALUES ($1, now())
               ON CONFLICT (event_id) DO NOTHING""",
            event_id,
        )
        claimed += int(result.split()[-1])  # "INSERT 0 1" | "INSERT 0 0"
    assert claimed == 1


async def test_ledger_primary_key_includes_partition_key(central_db: Any) -> None:
    """PK là `(event_id, occurred_at)` vì Postgres bắt buộc PK chứa cột phân vùng.

    Test này tồn tại để ai đó sửa PK về mỗi `event_id` sẽ thấy ngay lý do, thay vì gặp lỗi
    khó hiểu lúc chạy migration. Uniqueness toàn cục của `event_id` do `processed_event`
    đảm nhiệm, không phải bảng này.
    """
    columns = await central_db.fetch(
        """
        SELECT a.attname FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'point_ledger'::regclass AND i.indisprimary
        """
    )
    assert {r["attname"] for r in columns} == {"event_id", "occurred_at"}


# ═══════════════ Ràng buộc #6 và cấu hình có hệ quả CHECK ═══════════════


async def test_negative_balance_allowed_but_lifetime_earned_is_not(central_db: Any) -> None:
    """Hai cột, hai luật khác nhau — và đó là chủ đích.

    `balance` âm được phép vì `RETURN_ALLOW_NEGATIVE_BALANCE=true` (docs/05 §5): khách trả
    hàng sau khi đã tiêu điểm thì số dư âm là trạng thái đúng, không phải lỗi.

    `lifetime_earned` thì không bao giờ âm: nó là cơ sở XẾP HẠNG (ràng buộc #6). Cho phép
    nó âm là mở đường cho việc trả hàng làm tụt hạng khách.
    """
    import asyncpg

    await _seed(central_db)
    await central_db.execute(
        """INSERT INTO point_balance (customer_id, balance, lifetime_earned, tier)
           VALUES ($1, -20, 53, 'STANDARD')""",
        CUSTOMER_ID,
    )
    assert await central_db.fetchval("SELECT balance FROM point_balance") == -20

    with pytest.raises(asyncpg.CheckViolationError, match="lifetime_earned"):
        await central_db.execute(
            "UPDATE point_balance SET lifetime_earned = -1 WHERE customer_id = $1", CUSTOMER_ID
        )


async def test_merged_customer_must_point_somewhere(central_db: Any) -> None:
    """C03: gộp khách trùng. Bản ghi cũ không bị xóa — điểm của nó đã nằm trong ledger."""
    import asyncpg

    await _seed(central_db)
    with pytest.raises(asyncpg.CheckViolationError, match="customer_merged_has_target"):
        await central_db.execute(
            "UPDATE customer SET status = 'MERGED' WHERE customer_id = $1", CUSTOMER_ID
        )


async def test_replica_has_recorded_at_index_for_incremental_extract(central_db: Any) -> None:
    """Ràng buộc #3 + #8: bronze phân vùng theo `recorded_at`, job đối soát quét tăng dần.

    Cả hai đều quét theo watermark trên `recorded_at`. Thiếu index này thì mỗi lần trích
    xuất là một seq scan toàn bảng — không khả thi ở T3.
    """
    exists = await central_db.fetchval(
        """SELECT count(*) FROM pg_indexes
           WHERE tablename = 'sale_replica' AND indexdef LIKE '%recorded_at%'"""
    )
    assert exists >= 1
