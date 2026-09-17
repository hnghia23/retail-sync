"""Bất biến schema cửa hàng — docs/05 §6, docs/16 §5 (DI-2, DI-3).

Những test này kiểm **tầng dữ liệu**, không kiểm ứng dụng. Lý do: nguyên tắc ở docs/05 §6
là "không bao giờ chỉ dựa vào tầng ứng dụng". Nếu bất biến chỉ đúng khi đi qua use case,
thì một script sửa dữ liệu lúc 2 giờ sáng sẽ phá nó mà không ai biết.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

SHIFT_ID = uuid.UUID("01935e1c-0000-7000-8000-000000000001")
SALE_ID = uuid.UUID("01935e2a-0000-7000-8000-000000000001")


async def _open_shift(db: Any, store_id: str = "store-001") -> uuid.UUID:
    shift_id = uuid.uuid4()
    await db.execute(
        """
        INSERT INTO shift (shift_id, store_id, business_date, opened_by_employee_id, opening_cash)
        VALUES ($1, $2, DATE '2026-09-17', 'emp-007', 500000)
        """,
        shift_id,
        store_id,
    )
    return shift_id


async def _insert_sale(
    db: Any, shift_id: uuid.UUID, *, sale_id: uuid.UUID | None = None, **overrides: Any
) -> uuid.UUID:
    sid = sale_id or uuid.uuid4()
    values: dict[str, Any] = {
        "subtotal": 100_000,
        "discount_tier": 0,
        "discount_promo": 0,
        "total": 100_000,
        "status": "COMPLETED",
        "original_sale_id": None,
    }
    values.update(overrides)
    await db.execute(
        """
        INSERT INTO sale (sale_id, store_id, shift_id, business_date, employee_id,
                          subtotal, discount_tier, discount_promo, total, status, original_sale_id)
        VALUES ($1, 'store-001', $2, DATE '2026-09-17', 'emp-007', $3, $4, $5, $6, $7, $8)
        """,
        sid,
        shift_id,
        values["subtotal"],
        values["discount_tier"],
        values["discount_promo"],
        values["total"],
        values["status"],
        values["original_sale_id"],
    )
    return sid


# ═══════════════════════ Đường chính ═══════════════════════


async def test_valid_sale_with_split_payment_commits(edge_db: Any) -> None:
    """Cổng tuần 2: một đơn, tiền mặt + thẻ, SUM(sale_payment) = sale.total."""
    shift_id = await _open_shift(edge_db)
    async with edge_db.transaction():
        sale_id = await _insert_sale(
            edge_db, shift_id, subtotal=560_000, discount_tier=28_000, total=532_000
        )
        await edge_db.executemany(
            """INSERT INTO sale_line (sale_id, line_no, product_id, quantity,
                                      unit_price, line_total)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            [
                (sale_id, 1, "SKU-001", 2, 150_000, 300_000),
                (sale_id, 2, "SKU-002", 1, 160_000, 160_000),
                (sale_id, 3, "SKU-003", 1, 100_000, 100_000),
            ],
        )
        await edge_db.executemany(
            "INSERT INTO sale_payment (sale_id, seq, method, amount) VALUES ($1, $2, $3, $4)",
            [(sale_id, 1, "CASH", 300_000), (sale_id, 2, "CARD", 232_000)],
        )

    assert await edge_db.fetchval("SELECT count(*) FROM sale") == 1


async def test_return_sale_may_be_negative(edge_db: Any) -> None:
    """Trả 1 trong 3 món: `quantity`, `line_total`, `total` và khoản hoàn tiền đều âm.

    Đây là lý do `CHECK (total >= 0)` ở docs/05 §3.3 được nới thành
    `status = 'RETURN' OR total >= 0` — xem docstring của migration.
    """
    shift_id = await _open_shift(edge_db)
    async with edge_db.transaction():
        original = await _insert_sale(edge_db, shift_id)
        await edge_db.execute(
            """INSERT INTO sale_line (sale_id, line_no, product_id, quantity,
                                      unit_price, line_total)
               VALUES ($1, 1, 'SKU-002', 1, 100000, 100000)""",
            original,
        )
        await edge_db.execute(
            """INSERT INTO sale_payment (sale_id, seq, method, amount)
               VALUES ($1, 1, 'CASH', 100000)""",
            original,
        )

    async with edge_db.transaction():
        ret = await _insert_sale(
            edge_db,
            shift_id,
            subtotal=-100_000,
            total=-100_000,
            status="RETURN",
            original_sale_id=original,
        )
        await edge_db.execute(
            """INSERT INTO sale_line (sale_id, line_no, product_id, quantity, unit_price,
                                      line_total, original_sale_id, original_sale_line_no)
               VALUES ($1, 1, 'SKU-002', -1, 100000, -100000, $2, 1)""",
            ret,
            original,
        )
        await edge_db.execute(
            """INSERT INTO sale_payment (sale_id, seq, method, amount)
               VALUES ($1, 1, 'CASH', -100000)""",
            ret,
        )

    assert await edge_db.fetchval("SELECT count(*) FROM sale WHERE status = 'RETURN'") == 1


# ═══════════════════════ Bất biến phải CHẶN ═══════════════════════


async def test_inv1_rejects_bad_accounting(edge_db: Any) -> None:
    """INV-1 là CHECK trong một dòng → nổ ngay tại INSERT, không đợi commit."""
    import asyncpg

    shift_id = await _open_shift(edge_db)
    with pytest.raises(asyncpg.CheckViolationError, match="sale_inv1_accounting"):
        await _insert_sale(edge_db, shift_id, subtotal=560_000, discount_tier=28_000, total=500_000)


async def test_inv2_fires_at_commit_not_at_insert(edge_db: Any) -> None:
    """Đơn không có thanh toán nào.

    Khẳng định quan trọng nằm ở *thời điểm*: `INSERT INTO sale` phải THÀNH CÔNG (lúc đó
    chưa có dòng thanh toán nào — hoàn toàn bình thường giữa chừng transaction), và chỉ
    khi COMMIT trigger hoãn mới nổ. Nếu test này fail ở dòng INSERT thay vì ở commit,
    nghĩa là ai đó đã bỏ `DEFERRABLE INITIALLY DEFERRED` và luồng chốt đơn sẽ chết.
    """
    import asyncpg

    shift_id = await _open_shift(edge_db)
    tx = edge_db.transaction()
    await tx.start()
    sale_id = await _insert_sale(edge_db, shift_id)  # phải qua được
    await edge_db.execute(
        """INSERT INTO sale_line (sale_id, line_no, product_id, quantity, unit_price, line_total)
           VALUES ($1, 1, 'SKU-001', 1, 100000, 100000)""",
        sale_id,
    )

    with pytest.raises(asyncpg.RaiseError, match="INV-2"):
        await tx.commit()


async def test_inv3_rejects_line_total_mismatch(edge_db: Any) -> None:
    """DI-2: SUM(sale_line.line_total) phải bằng sale.subtotal. Lệch 1 đồng cũng chặn."""
    import asyncpg

    shift_id = await _open_shift(edge_db)
    tx = edge_db.transaction()
    await tx.start()
    sale_id = await _insert_sale(edge_db, shift_id)
    await edge_db.execute(
        """INSERT INTO sale_line (sale_id, line_no, product_id, quantity, unit_price, line_total)
           VALUES ($1, 1, 'SKU-001', 1, 100000, 99999)""",
        sale_id,
    )
    await edge_db.execute(
        "INSERT INTO sale_payment (sale_id, seq, method, amount) VALUES ($1, 1, 'CASH', 100000)",
        sale_id,
    )

    with pytest.raises(asyncpg.RaiseError, match="INV-3"):
        await tx.commit()


async def test_completed_sale_cannot_be_negative(edge_db: Any) -> None:
    import asyncpg

    shift_id = await _open_shift(edge_db)
    with pytest.raises(asyncpg.CheckViolationError, match="sale_total_sign"):
        await _insert_sale(edge_db, shift_id, subtotal=-1000, total=-1000)


async def test_only_one_open_shift_per_store(edge_db: Any) -> None:
    """Hai ca cùng mở thì `POST /sales` không biết ghi vào đâu và báo cáo ca sẽ thiếu đơn."""
    import asyncpg

    await _open_shift(edge_db)
    with pytest.raises(asyncpg.UniqueViolationError, match="shift_one_open_per_store"):
        await _open_shift(edge_db)


async def test_different_stores_may_each_have_an_open_shift(edge_db: Any) -> None:
    """Ràng buộc trên là theo CỬA HÀNG, không phải toàn cục."""
    await _open_shift(edge_db, "store-001")
    await _open_shift(edge_db, "store-002")
    assert await edge_db.fetchval("SELECT count(*) FROM shift WHERE status = 'OPEN'") == 2


# ═══════════════════════ Ràng buộc vận hành ═══════════════════════


async def test_outbox_has_dedicated_autovacuum(edge_db: Any) -> None:
    """Ràng buộc #9. `outbox` là bảng DUY NHẤT có mẫu insert → update → delete."""
    reloptions = await edge_db.fetchval("SELECT reloptions FROM pg_class WHERE relname = 'outbox'")
    assert reloptions is not None, "outbox chưa được đặt tham số autovacuum riêng"
    settings = dict(opt.split("=", 1) for opt in reloptions)
    assert settings["autovacuum_vacuum_scale_factor"] == "0.02"
    assert settings["autovacuum_vacuum_threshold"] == "50"


async def test_outbox_index_is_partial(edge_db: Any) -> None:
    """Index phải chỉ phủ dòng CHƯA gửi — nếu không, nó lớn bằng cả bảng."""
    predicate = await edge_db.fetchval(
        """SELECT pg_get_expr(indpred, indrelid) FROM pg_index
           WHERE indexrelid = 'outbox_unsent_idx'::regclass"""
    )
    assert predicate is not None, "outbox_unsent_idx không phải partial index"
    assert "sent_at IS NULL" in predicate


async def test_uuidv7_is_time_ordered(edge_db: Any) -> None:
    """Spike S2 (docs/06 ngày 0): `uuidv7()` native của PG 18.

    Tính sắp theo thời gian mới là lý do chọn v7 thay vì v4 — insert nhanh hơn ~10×, index
    nhỏ hơn ~25% (docs/05 §1). Nếu test này fail, ID vẫn duy nhất nhưng lợi ích đã mất.
    """
    ids = []
    for i in range(5):
        ids.append(
            await edge_db.fetchval(
                """INSERT INTO outbox (event_type, payload) VALUES ($1, '{}'::jsonb)
               RETURNING event_id""",
                f"Event{i}",
            )
        )
    assert ids == sorted(ids, key=str)
    assert ids[0].version == 7


async def test_point_ledger_local_rejects_zero_delta(edge_db: Any) -> None:
    """Dòng ledger delta = 0 là rác: không thay đổi gì nhưng vẫn phải đồng bộ và đối soát."""
    import asyncpg

    await edge_db.execute(
        "INSERT INTO customer_local (customer_id, phone_hash) VALUES ($1, 'h1')", uuid.uuid4()
    )
    customer_id = await edge_db.fetchval("SELECT customer_id FROM customer_local")
    with pytest.raises(asyncpg.CheckViolationError):
        await edge_db.execute(
            """INSERT INTO point_ledger_local (customer_id, store_id, delta, reason)
               VALUES ($1, 'store-001', 0, 'EARN')""",
            customer_id,
        )
