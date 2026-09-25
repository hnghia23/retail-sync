"""Watermark trích xuất — docs/17-data-flow.md §4 bẫy 1, 2, 3 (migration 0004).

Mỗi bẫy ở đây làm dữ liệu lọt khỏi warehouse MÀ KHÔNG CÓ LỖI NÀO, nên mỗi test phải dựng
lại đúng tình huống lọt dòng, không chỉ kiểm "cột có tồn tại".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

CUSTOMER_ID = uuid.UUID("01935abc-0000-7000-8000-00000000c001")


async def _seed(db: Any) -> None:
    await db.execute("INSERT INTO region (region_id, name) VALUES ('north', 'Mien Bac')")
    await db.execute(
        "INSERT INTO store (store_id, name, region_id) VALUES ('store-001', 'CH 001', 'north')"
    )


async def _insert_sale(db: Any) -> uuid.UUID:
    sale_id = uuid.uuid4()
    await db.execute(
        """
        INSERT INTO sale_replica (sale_id, store_id, business_date, employee_id, occurred_at,
                                  subtotal, total)
        VALUES ($1, 'store-001', current_date, 'emp-1', now(), 100000, 100000)
        """,
        sale_id,
    )
    return sale_id


@pytest.fixture
async def other_conn(central_db: Any, postgres_dsn: str) -> AsyncIterator[Any]:
    """Phiên thứ hai vào CÙNG DB trung tâm — đóng vai một lô ingest đang chạy."""
    import asyncpg

    dbname = await central_db.fetchval("SELECT current_database()")
    conn = await asyncpg.connect(postgres_dsn.rsplit("/", 1)[0] + f"/{dbname}")
    try:
        yield conn
    finally:
        await conn.close()


# ═══════════════ Bẫy 1 — transaction commit muộn ═══════════════


async def test_late_commit_is_lost_by_naive_watermark_but_not_by_horizon(
    central_db: Any, other_conn: Any
) -> None:
    """Dựng lại đúng kịch bản lọt dòng ở docs/17 §4 bẫy 1.

    Một lô ingest mở transaction, ghi `sale_replica`, CHƯA commit. Trích xuất chạy lúc đó:
    - mốc ngây thơ `now()` đã VƯỢT `recorded_at` của dòng đang ghi dở → cửa sổ kế tiếp bắt
      đầu từ mốc đó, dòng commit sau sẽ nằm dưới mép dưới → lọt vĩnh viễn;
    - `extract_horizon()` dừng ở `xact_start` của lô đang mở → dòng rơi vào cửa sổ sau.
    """
    await _seed(central_db)

    ingest = other_conn.transaction()
    await ingest.start()
    sale_id = await _insert_sale(other_conn)
    row_recorded_at = await other_conn.fetchval(
        "SELECT recorded_at FROM sale_replica WHERE sale_id = $1", sale_id
    )

    naive_end = await central_db.fetchval("SELECT clock_timestamp()")
    horizon = await central_db.fetchval("SELECT extract_horizon(interval '0 seconds')")
    visible_now = await central_db.fetchval("SELECT count(*) FROM sale_replica")

    await ingest.commit()

    assert visible_now == 0, "dòng chưa commit mà trích xuất đã thấy — sai giả định của test"
    # Chính cái bẫy: cửa sổ kế tiếp [naive_end, ...) sẽ không bao giờ chứa dòng này.
    assert row_recorded_at < naive_end
    # Cách chữa: mốc không vượt quá dòng còn đang ghi dở.
    assert horizon <= row_recorded_at

    # Cửa sổ kế tiếp [horizon, horizon mới) chứa đúng dòng đó.
    next_horizon = await central_db.fetchval("SELECT extract_horizon(interval '0 seconds')")
    picked = await central_db.fetchval(
        "SELECT count(*) FROM sale_replica WHERE recorded_at >= $1 AND recorded_at < $2",
        horizon,
        next_horizon,
    )
    assert picked == 1


async def test_horizon_holds_while_a_transaction_is_open(central_db: Any, other_conn: Any) -> None:
    """Transaction treo làm trích xuất ĐỨNG, không làm trích xuất BỎ QUA.

    Kể cả phiên "idle in transaction" (mở rồi bỏ đó, như một client quên commit) cũng giữ
    mốc lại. Trần độ dài là việc của `transaction_timeout` ở Central API, không phải hàm này.
    """
    tx = other_conn.transaction()
    await tx.start()
    started = await other_conn.fetchval("SELECT now()")

    first = await central_db.fetchval("SELECT extract_horizon(interval '0 seconds')")
    await central_db.execute("SELECT pg_sleep(0.2)")
    second = await central_db.fetchval("SELECT extract_horizon(interval '0 seconds')")
    await tx.rollback()
    released = await central_db.fetchval("SELECT extract_horizon(interval '0 seconds')")

    assert first <= started and second <= started
    assert released > started


async def test_horizon_without_open_transactions_is_now_minus_lag(central_db: Any) -> None:
    before = await central_db.fetchval("SELECT clock_timestamp()")
    horizon = await central_db.fetchval("SELECT extract_horizon(interval '30 seconds')")
    after = await central_db.fetchval("SELECT clock_timestamp()")

    assert before - timedelta(seconds=30) <= horizon <= after - timedelta(seconds=30)


async def test_horizon_ignores_the_callers_own_transaction(central_db: Any) -> None:
    """Bộ trích xuất đọc trong transaction của chính nó — không được tự chặn mình."""
    async with central_db.transaction():
        own_start = await central_db.fetchval("SELECT now()")
        await central_db.execute("SELECT pg_sleep(0.2)")
        horizon = await central_db.fetchval("SELECT extract_horizon(interval '0 seconds')")
    assert horizon > own_start


async def test_horizon_refuses_to_guess_without_privilege(
    central_db: Any, other_conn: Any, postgres_dsn: str
) -> None:
    """Thiếu `pg_read_all_stats` thì `xact_start` của phiên khác là NULL, `min()` bỏ qua nó
    và trả về một mốc SAI mà không báo gì. Hàm phải từ chối, không được im lặng."""
    import asyncpg

    dbname = await central_db.fetchval("SELECT current_database()")
    role = f"extractor_{uuid.uuid4().hex[:8]}"
    await central_db.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'x'")
    base = postgres_dsn.rsplit("@", 1)[1].rsplit("/", 1)[0]

    tx = other_conn.transaction()
    await tx.start()
    await other_conn.execute("SELECT 1")
    try:
        weak = await asyncpg.connect(f"postgresql://{role}:x@{base}/{dbname}")
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError, match="pg_read_all_stats"):
                await weak.fetchval("SELECT extract_horizon()")
        finally:
            await weak.close()

        await central_db.execute(f"GRANT pg_read_all_stats TO {role}")
        strong = await asyncpg.connect(f"postgresql://{role}:x@{base}/{dbname}")
        try:
            assert await strong.fetchval("SELECT extract_horizon()") is not None
        finally:
            await strong.close()
    finally:
        await tx.rollback()
        await central_db.execute(f"DROP ROLE {role}")


# ═══════════════ Bẫy 2 — index `recorded_at` trên mọi partition ═══════════════


async def test_every_ledger_partition_has_recorded_at_index(central_db: Any) -> None:
    """Kể cả partition tạo SAU migration — job tạo partition chạy hằng ngày ở production."""
    await central_db.fetchval("SELECT ensure_point_ledger_partitions(8)")
    missing = await central_db.fetch(
        """
        SELECT c.relname
          FROM pg_inherits i
          JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = 'point_ledger'::regclass
           AND NOT EXISTS (
               SELECT 1 FROM pg_index x
                 JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = x.indkey[0]
                WHERE x.indrelid = c.oid AND a.attname = 'recorded_at'
           )
        """
    )
    partitions = await central_db.fetchval(
        "SELECT count(*) FROM pg_inherits WHERE inhparent = 'point_ledger'::regclass"
    )
    assert partitions >= 9
    assert [r["relname"] for r in missing] == []


async def test_ledger_extraction_query_uses_the_index(central_db: Any) -> None:
    """Truy vấn trích xuất phải đi được bằng index, không quét trọn partition.

    Tắt seq scan để planner không chọn quét tuần tự chỉ vì bảng test rỗng — câu hỏi ở đây
    là "có ĐƯỜNG đi bằng index không", không phải "planner chọn gì trên 0 dòng".
    """
    await central_db.execute("SET enable_seqscan = off")
    rows = await central_db.fetch(
        """
        EXPLAIN SELECT * FROM point_ledger
         WHERE recorded_at >= now() - interval '1 hour' AND recorded_at < now()
        """
    )
    text_plan = "\n".join(r[0] for r in rows)
    assert "Seq Scan" not in text_plan
    assert "recorded_at" in text_plan


# ═══════════════ Bẫy 3 — mọi lần ghi chạm cột watermark ═══════════════


async def test_customer_update_moves_recorded_at(central_db: Any) -> None:
    """`CustomerUpdated` sửa dòng tại chỗ — bản cập nhật phải lọt vào cửa sổ trích xuất sau."""
    await central_db.execute(
        "INSERT INTO customer (customer_id, phone_hash) VALUES ($1, 'h1')", CUSTOMER_ID
    )
    first = await central_db.fetchval(
        "SELECT recorded_at FROM customer WHERE customer_id = $1", CUSTOMER_ID
    )
    await central_db.execute(
        "UPDATE customer SET name_enc = 'x'::bytea WHERE customer_id = $1", CUSTOMER_ID
    )
    second = await central_db.fetchval(
        "SELECT recorded_at FROM customer WHERE customer_id = $1", CUSTOMER_ID
    )
    assert second > first


@pytest.mark.parametrize(
    ("table", "column", "insert_sql", "update_sql"),
    [
        (
            "customer",
            "recorded_at",
            "INSERT INTO customer (customer_id, phone_hash, recorded_at)"
            " VALUES ($1, 'h1', '2000-01-01')",
            "UPDATE customer SET recorded_at = '2000-01-01' WHERE customer_id = $1",
        ),
        (
            "sale_replica",
            "recorded_at",
            "INSERT INTO sale_replica (sale_id, store_id, business_date, employee_id,"
            " occurred_at, subtotal, total, recorded_at)"
            " VALUES ($1, 'store-001', current_date, 'e', now(), 1, 1, '2000-01-01')",
            "UPDATE sale_replica SET status = 'VOIDED', recorded_at = '2000-01-01'"
            " WHERE sale_id = $1",
        ),
        (
            "shift_replica",
            "recorded_at",
            "INSERT INTO shift_replica (shift_id, store_id, business_date,"
            " opened_by_employee_id, opened_at, recorded_at)"
            " VALUES ($1, 'store-001', current_date, 'e', now(), '2000-01-01')",
            "UPDATE shift_replica SET variance = 0, recorded_at = '2000-01-01' WHERE shift_id = $1",
        ),
    ],
    ids=["customer", "sale_replica", "shift_replica"],
)
async def test_watermark_cannot_be_forged_or_forgotten(
    central_db: Any, table: str, column: str, insert_sql: str, update_sql: str
) -> None:
    """Handler quên set cột, hoặc cố set giá trị cũ → trigger vẫn đặt giờ trung tâm.

    `sale_replica` hôm nay chỉ bị INSERT, nhưng `SaleVoided` sau này sẽ UPDATE `status` —
    đúng loại thay đổi mà bẫy 3 làm lọt nếu phải nhớ set cột ở từng handler.
    """
    await _seed(central_db)
    key = uuid.uuid4()
    id_column = {"customer": "customer_id", "sale_replica": "sale_id"}.get(table, "shift_id")

    # Tên bảng/cột lấy từ bộ tham số cố định của test, không từ dữ liệu ngoài.
    select = f"SELECT {column} FROM {table} WHERE {id_column} = $1"  # noqa: S608

    await central_db.execute(insert_sql, key)
    inserted = await central_db.fetchval(select, key)
    await central_db.execute(update_sql, key)
    updated = await central_db.fetchval(select, key)

    assert inserted.year > 2000
    assert updated >= inserted


async def test_point_balance_upsert_moves_updated_at(central_db: Any) -> None:
    await central_db.execute(
        "INSERT INTO customer (customer_id, phone_hash) VALUES ($1, 'h1')", CUSTOMER_ID
    )
    await central_db.execute(
        "INSERT INTO point_balance (customer_id, balance, updated_at)"
        " VALUES ($1, 10, '2000-01-01')",
        CUSTOMER_ID,
    )
    first = await central_db.fetchval(
        "SELECT updated_at FROM point_balance WHERE customer_id = $1", CUSTOMER_ID
    )
    await central_db.execute(
        "UPDATE point_balance SET balance = 20 WHERE customer_id = $1", CUSTOMER_ID
    )
    second = await central_db.fetchval(
        "SELECT updated_at FROM point_balance WHERE customer_id = $1", CUSTOMER_ID
    )
    assert first.year > 2000
    assert second > first


async def test_transaction_timeout_actually_ends_a_hung_transaction(
    central_db: Any, postgres_dsn: str
) -> None:
    """`make_engine(server_settings=...)` phải tới được Postgres, và Postgres phải thật sự cắt.

    Nếu tham số không tới nơi (sai tên, sai cách truyền), transaction treo sẽ giữ mốc
    `extract_horizon()` vô thời hạn — trích xuất đứng mà không ai cắt.

    Postgres cắt bằng cách ĐÓNG HẲN phiên (FATAL), nên client thấy "connection was closed"
    chứ không phải một lỗi SQL có tên. Thước đo đúng là thời gian: bị cắt trước khi
    `pg_sleep(2)` kịp xong. Kết nối chết trong pool được `pool_pre_ping` loại ở lần mượn sau.
    """
    import time

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from shared.db import make_engine, make_session_factory

    dbname = await central_db.fetchval("SELECT current_database()")
    base = postgres_dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
    engine = make_engine(f"{base}/{dbname}", server_settings={"transaction_timeout": "300ms"})
    try:
        factory = make_session_factory(engine)
        started = time.monotonic()
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(text("SELECT 1"))
                await session.execute(text("SELECT pg_sleep(2)"))
        assert time.monotonic() - started < 1.5

        # Pool tự hồi phục: kết nối kế tiếp dùng được bình thường.
        async with factory() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()
