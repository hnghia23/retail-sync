"""S4 + S5 trên Postgres + MinIO + ClickHouse thật — docs/17 §2, §4.

Mỗi test dựng lại một cách pipeline có thể sai mà KHÔNG báo lỗi: nạp thiếu, nạp đôi, lọt
dòng commit muộn, mất bản cập nhật, không dựng lại được từ lake.

Cửa sổ trích xuất 1 giây và độ trễ an toàn 0, để một lượt test không phải chờ một giờ.
"""
# SQL trong file này chỉ ghép tên bảng/hằng của chính test, không có dữ liệu ngoài.
# ruff: noqa: S608

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import uuid
from typing import Any

import pytest

from pipeline import run as pipeline_run
from pipeline.clickhouse import ClickHouse
from pipeline.lake import Lake
from pipeline.run import LOCK_KEY, PipelineBusyError, run_once
from pipeline.tables import INCREMENTAL, SNAPSHOT, TABLES
from tests.integration.conftest import Env

STORE = "store-001"
CUSTOMER = uuid.UUID("01935abc-0000-7000-8000-00000000d001")


async def _seed(db: Any, *, sales: int = 5) -> None:
    await db.execute("INSERT INTO region (region_id, name) VALUES ('north', 'Mien Bac')")
    await db.execute(
        f"INSERT INTO store (store_id, name, region_id) VALUES ('{STORE}', 'CH 001', 'north')"
    )
    await db.execute(
        "INSERT INTO employee (employee_id, store_id, name, role, password_hash)"
        f" VALUES ('e1', '{STORE}', 'Thu ngan', 'cashier', 'x')"
    )
    await db.execute("INSERT INTO customer (customer_id, phone_hash) VALUES ($1, 'h')", CUSTOMER)
    shift = uuid.uuid4()
    await db.execute(
        "INSERT INTO shift_replica (shift_id, store_id, business_date, opened_by_employee_id,"
        f" opened_at, closed_at, expected_cash, counted_cash, variance)"
        f" VALUES ($1, '{STORE}', current_date, 'e1', now(), now(), 0, 0, 0)",
        shift,
    )
    for i in range(sales):
        await _sale(db, shift, amount=100_000 * (i + 1))


async def _sale(db: Any, shift: uuid.UUID, *, amount: int) -> uuid.UUID:
    sale = uuid.uuid4()
    await db.execute(
        "INSERT INTO sale_replica (sale_id, store_id, shift_id, business_date, employee_id,"
        " customer_id, occurred_at, subtotal, total)"
        f" VALUES ($1, '{STORE}', $2, current_date, 'e1', $3, now(), $4, $4)",
        sale,
        shift,
        CUSTOMER,
        amount,
    )
    await db.execute(
        "INSERT INTO sale_line_replica (sale_id, line_no, product_id, quantity, unit_price,"
        " line_total) VALUES ($1, 1, 'P1', 1, $2, $2)",
        sale,
        amount,
    )
    await db.execute(
        "INSERT INTO sale_payment_replica (sale_id, seq, method, amount)"
        " VALUES ($1, 1, 'CASH', $2)",
        sale,
        amount,
    )
    await db.execute(
        "INSERT INTO point_ledger (event_id, customer_id, store_id, sale_id, delta, reason,"
        f" occurred_at) VALUES ($1, $2, '{STORE}', $3, $4, 'EARN', now())",
        uuid.uuid4(),
        CUSTOMER,
        sale,
        amount // 10_000,
    )
    await db.execute(
        "INSERT INTO point_balance (customer_id, balance, lifetime_earned) VALUES ($1, $2, $2)"
        " ON CONFLICT (customer_id) DO UPDATE SET balance = point_balance.balance + $2,"
        " lifetime_earned = point_balance.lifetime_earned + $2",
        CUSTOMER,
        amount // 10_000,
    )
    return sale


async def _pg_counts(db: Any) -> dict[str, int]:
    source = {
        "sale": "sale_replica",
        "sale_line": "sale_line_replica",
        "sale_payment": "sale_payment_replica",
        "point_ledger": "point_ledger",
        "shift": "shift_replica",
        "customer": "customer",
        "point_balance": "point_balance",
    }
    return {t: int(await db.fetchval(f"SELECT count(*) FROM {src}")) for t, src in source.items()}


async def _settle() -> None:
    """Qua hết cửa sổ 1 giây chứa dòng vừa ghi — để mép cuối ≤ extract_horizon()."""
    await asyncio.sleep(1.2)


# ═══════════════ Đặc tả ↔ DDL ═══════════════


def test_every_spec_column_exists_in_its_bronze_table(env: Env) -> None:
    """Thêm cột vào đặc tả mà quên DDL (hoặc ngược lại) → cột đó mất âm thầm."""
    for spec in TABLES:
        rows = env.ch.rows(
            "SELECT name FROM system.columns"
            f" WHERE database = currentDatabase() AND table = '{spec.bronze}'"
        )
        bronze = {r[0] for r in rows} - {"_dt", "_source_file"}
        assert bronze == {c.name for c in spec.columns}, spec.name


# ═══════════════ Đường thành công + chạy lại ═══════════════


async def test_everything_lands_in_bronze_and_rerunning_changes_nothing(env: Env) -> None:
    """AT-07 ở tầng S4/S5: chạy lại không nhân đôi, không trích lại, không nạp lại."""
    await _seed(env.db)
    await _settle()

    first = await env.run()
    pg = await _pg_counts(env.db)
    assert all(pg.values())
    for spec in INCREMENTAL:
        assert env.count(spec.bronze) == pg[spec.name], spec.name
    for spec in SNAPSHOT:
        assert env.count(spec.bronze) == int(
            await env.db.fetchval(f"SELECT count(*) FROM {spec.source}")
        ), spec.name
    assert first.loaded

    second = await env.run()
    # Thời gian đã trôi nên có thể có cửa sổ MỚI (rỗng) — nhưng không có dữ liệu nào bị trích
    # lại hay nạp lại.
    assert all(e.rows == 0 for e in second.extracted if e.window is not None and e.created)
    assert sum(item.rows for item in second.loaded) == 0
    for spec in INCREMENTAL:
        assert env.count(spec.bronze) == pg[spec.name], spec.name

    # DI-4: số dòng bronze theo từng file = số dòng nhật ký nạp ghi cho file đó.
    mismatch = env.ch.rows(
        "SELECT l.path FROM bronze_load_log l LEFT JOIN"
        " (SELECT _source_file, count() AS n FROM bronze_sale GROUP BY _source_file) b"
        " ON b._source_file = l.path WHERE l.table_name = 'sale' AND l.rows != b.n"
    )
    assert mismatch == []


async def test_lost_load_log_does_not_duplicate_bronze(env: Env) -> None:
    """Nhật ký nạp mất (hoặc chết giữa insert dữ liệu và insert nhật ký) → nạp lại mọi file,
    và token khử trùng chặn nhân đôi — lưới an toàn thứ hai, không chỉ dựa vào nhật ký."""
    await _seed(env.db)
    await _settle()
    await env.run()
    before = env.count("bronze_sale")

    env.ch.query("TRUNCATE TABLE bronze_load_log")
    again = await env.run()
    assert again.loaded  # đã thử nạp lại
    assert env.count("bronze_sale") == before


async def test_rebuild_warehouse_from_lake(env: Env) -> None:
    """Cổng A: xóa sạch bronze trong ClickHouse → dựng lại hoàn toàn từ lake, không đụng PG."""
    await _seed(env.db)
    await _settle()
    await env.run()
    counts = {spec.bronze: env.count(spec.bronze) for spec in TABLES}

    for spec in TABLES:
        env.ch.query(f"TRUNCATE TABLE {spec.bronze}")
    env.ch.query("TRUNCATE TABLE bronze_load_log")
    await env.db.execute("DELETE FROM sale_payment_replica")  # nguồn đổi cũng không sao

    report = await env.run()
    assert all(e.rows == 0 for e in report.extracted if e.window is not None and e.created)
    assert {spec.bronze: env.count(spec.bronze) for spec in TABLES} == counts


async def test_empty_table_still_leaves_an_anchor_in_the_load_log(env: Env) -> None:
    """Bảng rỗng không có file → vắng khỏi nhật ký nạp → mép "đã nạp tới" (min trên mọi bảng)
    của dbt và audit L3 không tính được. Phải có một file rỗng làm mốc, và dòng đến sau vẫn vào."""
    first = await env.run()
    for spec in INCREMENTAL:
        files = [e for e in first.extracted if e.table == spec.name]
        assert [e.rows for e in files] == [0], spec.name
    logged = {r[0] for r in env.ch.rows("SELECT DISTINCT table_name FROM bronze_load_log")}
    assert {spec.name for spec in INCREMENTAL} <= logged

    await _seed(env.db)
    await _settle()
    await env.run()
    pg = await _pg_counts(env.db)
    for spec in INCREMENTAL:
        assert env.count(spec.bronze) == pg[spec.name], spec.name


# ═══════════════ Mỗi lúc chỉ một lượt ═══════════════


async def test_second_run_stops_while_another_holds_the_lock(env: Env) -> None:
    """DAG và `make pipeline-run` chạy chồng nhau → lượt sau dừng NGAY, không đụng lake."""
    await _seed(env.db)
    await _settle()
    await env.db.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
    try:
        with pytest.raises(PipelineBusyError):
            await env.run()
        assert all(not env.lake.list_table(spec.name) for spec in TABLES)
    finally:
        await env.db.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)

    assert (await env.run()).loaded  # khóa nhả → lượt sau chạy bình thường


async def test_lock_is_held_through_loading_not_only_extraction(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nhả khóa trước khi nạp thì một lượt khác chen vào giữa lúc nạp của lượt này."""
    await _seed(env.db)
    await _settle()
    seen: list[bool] = []
    from pipeline.load import load_pending as real

    def probing(ch: ClickHouse, lake: Lake, spec: Any) -> Any:
        got = asyncio.run_coroutine_threadsafe(
            env.db.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY), loop
        )
        seen.append(got.result(timeout=10))
        return real(ch, lake, spec)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(pipeline_run, "load_pending", probing)
    # `load_pending` đồng bộ, chạy trong luồng riêng để thăm dò khóa từ vòng lặp này.
    await asyncio.to_thread(asyncio.run, run_once(env.cfg, env.lake, env.ch))
    await env.db.execute("SELECT pg_advisory_unlock_all()")
    assert seen and not any(seen), "có lúc nạp mà khóa đang trống"


# ═══════════════ Bẫy 1 và 3 đi hết đường tới ClickHouse ═══════════════


async def test_late_committing_row_is_not_lost(env: Env, postgres_dsn: str) -> None:
    """Bẫy 1 đầu-cuối: một lô ingest đang mở khi pipeline chạy → pipeline DỪNG ở mép của nó
    thay vì trích qua, và dòng đó có mặt ở bronze ở lượt sau."""
    import asyncpg

    await _seed(env.db, sales=1)
    await _settle()
    await env.run()

    ingest = await asyncpg.connect(env.cfg.pg_dsn)
    try:
        tx = ingest.transaction()
        await tx.start()
        shift = await ingest.fetchval("SELECT shift_id FROM shift_replica LIMIT 1")
        late = await _sale(ingest, shift, amount=777_000)
        await asyncio.sleep(1.5)  # vài cửa sổ trôi qua trong lúc lô chưa commit

        await env.run()
        await tx.commit()
    finally:
        await ingest.close()

    await _settle()
    await env.run()
    found = env.ch.query(f"SELECT count() FROM bronze_sale WHERE sale_id = toUUID('{late}')")
    assert found == "1"


async def test_updated_row_arrives_as_a_new_version_and_old_file_is_untouched(env: Env) -> None:
    """Bẫy 3 đầu-cuối: UPDATE ở trung tâm → phiên bản mới ở cửa sổ sau; file cũ BẤT BIẾN.

    Cửa sổ cũ vẫn còn các đơn KHÁC (5 đơn ghi cùng lúc, chỉ sửa một): một bộ trích xuất không
    giữ trạng thái sẽ trích lại cửa sổ đó, ra nội dung khác (thiếu đơn đã sửa), và ghi đè file
    mà bronze đã nạp — lake hết là bản sao bất biến.
    """
    await _seed(env.db, sales=5)
    await _settle()
    await env.run()
    old_files = env.ch.rows(
        "SELECT path, sha256 FROM bronze_load_log WHERE table_name IN ('customer', 'sale')"
    )
    changed = await env.db.fetchval("SELECT sale_id FROM sale_replica ORDER BY sale_id LIMIT 1")

    await env.db.execute("UPDATE customer SET status = 'BLOCKED' WHERE customer_id = $1", CUSTOMER)
    await env.db.execute("UPDATE sale_replica SET status = 'VOIDED' WHERE sale_id = $1", changed)
    await _settle()
    await env.run()

    for path, sha in old_files:
        assert env.lake.sha256(path) == sha, f"file đã nạp bị ghi đè: {path}"
    versions = env.ch.query(
        f"SELECT count() FROM bronze_customer WHERE customer_id = toUUID('{CUSTOMER}')"
    )
    latest = env.ch.query(
        "SELECT argMax(status, recorded_at) FROM bronze_customer"
        f" WHERE customer_id = toUUID('{CUSTOMER}')"
    )
    assert (versions, latest) == ("2", "BLOCKED")
    sale_latest = env.ch.query(
        f"SELECT argMax(status, recorded_at) FROM bronze_sale WHERE sale_id = toUUID('{changed}')"
    )
    assert sale_latest == "VOIDED"
    assert env.count("bronze_sale") == 6  # 5 đơn + 1 phiên bản mới


async def test_changing_window_size_never_overlaps(env: Env) -> None:
    """Cửa sổ mới luôn bắt đầu ở mép cuối của file cuối — đổi độ dài không tạo chồng lấn."""
    await _seed(env.db, sales=2)
    await _settle()
    await env.run()
    env.cfg = dataclasses.replace(env.cfg, window_seconds=3)
    await asyncio.sleep(3.2)
    await env.run()

    windows = sorted(
        (r[0], r[1])
        for r in env.ch.rows(
            "SELECT window_start, window_end FROM bronze_load_log WHERE table_name = 'sale'"
        )
    )
    for (_, prev_end), (start, _) in itertools.pairwise(windows):
        assert start == prev_end
