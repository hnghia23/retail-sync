"""Bronze ClickHouse — docs/17-data-flow.md §4 bẫy 4, kiểm trên đúng image của compose.

Chạy lại một lần nạp là chuyện thường ngày (DAG retry, backfill, người vận hành bấm lại).
Mọi test ở đây dựng lại một cách chạy lại cụ thể và kiểm số dòng — vì khi khử trùng hỏng,
ClickHouse KHÔNG báo lỗi gì, chỉ âm thầm nhân đôi hoặc âm thầm bỏ dữ liệu.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from tests.integration.conftest import ROOT, ClickHouse

BRONZE_SQL = ROOT / "packages" / "pipeline" / "ddl" / "bronze.sql"

# Ép INSERT ... SELECT tách thành nhiều khối — giống một file Parquet lớn. ClickHouse gắn
# hậu tố số thứ tự khối vào token, nên chạy lại phải tách khối Y HỆT lần đầu.
MULTI_BLOCK = {
    "max_block_size": 100,
    "max_insert_block_size": 100,
    "min_insert_block_size_rows": 0,
    "min_insert_block_size_bytes": 0,
}


def _statements() -> list[str]:
    body = "\n".join(
        line
        for line in BRONZE_SQL.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    return [s.strip() for s in body.split(";") if s.strip()]


class Db:
    """Một database ClickHouse riêng cho từng test."""

    def __init__(self, ch: ClickHouse, name: str) -> None:
        self.ch = ch
        self.name = name

    def query(self, sql: str, **settings: object) -> str:
        return self.ch.query(sql, database=self.name, **settings)

    def count(self, table: str) -> int:
        return int(self.query(f"SELECT count() FROM {table}"))  # noqa: S608 — tên bảng là hằng


@pytest.fixture
def bronze(clickhouse: ClickHouse) -> Iterator[Db]:
    name = f"t_{uuid.uuid4().hex[:10]}"
    clickhouse.query(f"CREATE DATABASE {name}")
    db = Db(clickhouse, name)
    for statement in _statements():
        db.query(statement)
    try:
        yield db
    finally:
        clickhouse.query(f"DROP DATABASE {name}")


def _load_sales(db: Db, *, token: str, rows: int = 1000, offset: int = 0) -> None:
    """Một "lần nạp file" giả: nội dung tất định theo (rows, offset)."""
    sale_id = "toUUID(concat('00000000-0000-7000-8000-', leftPad(toString(number + {}), 12, '0')))"
    db.query(
        f"""
        INSERT INTO bronze_sale (sale_id, store_id, business_date, employee_id, occurred_at,
                                 recorded_at, subtotal, total, status, _dt, _source_file)
        SELECT {sale_id.format(offset)},
               'store-001', toDate32('2026-09-23'), 'emp-1',
               toDateTime64('2026-09-23 03:00:00', 6, 'UTC'),
               toDateTime64('2026-09-23 03:05:00', 6, 'UTC'),
               100000, 100000, 'COMPLETED', toDate('2026-09-23'), 'dt=2026-09-23/a.parquet'
        FROM numbers({rows})
        """,  # noqa: S608 — chỉ số nguyên do test đặt
        insert_deduplication_token=token,
        **MULTI_BLOCK,
    )


# ═══════════════ Cấu hình phải có trên MỌI bảng ═══════════════


def test_every_bronze_table_declares_a_dedup_window(bronze: Db) -> None:
    """Thêm bảng bronze mà quên dòng SETTINGS → test này đỏ, trước khi dữ liệu nhân đôi."""
    declared = [
        re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", s).group(1)  # type: ignore[union-attr]
        for s in _statements()
        if s.startswith("CREATE TABLE")
    ]
    rows = bronze.query(
        "SELECT name, engine_full FROM system.tables WHERE database = currentDatabase() FORMAT TSV"
    ).splitlines()
    engines = dict(line.split("\t", 1) for line in rows)

    assert sorted(engines) == sorted(declared)
    assert len(declared) >= 7
    missing = [t for t, e in engines.items() if "non_replicated_deduplication_window" not in e]
    assert missing == []


def test_without_a_window_the_token_is_silently_ignored(bronze: Db) -> None:
    """Đối chứng: chính cái bẫy, trên đúng bản ClickHouse đang dùng.

    Nếu một ngày test này ĐỎ (bản mới bật khử trùng mặc định), thì bẫy 4 đã hết — cập nhật
    docs/17 §4 thay vì xóa test.
    """
    bronze.query("CREATE TABLE plain (x UInt64) ENGINE = MergeTree ORDER BY x")
    for _ in range(2):
        bronze.query("INSERT INTO plain VALUES (1), (2), (3)", insert_deduplication_token="t")
    assert bronze.count("plain") == 6


# ═══════════════ Ba quy tắc cho bộ nạp ═══════════════


def test_rerunning_the_same_load_does_not_duplicate(bronze: Db) -> None:
    """AT-07 ở tầng bronze: nạp lại cùng file (nhiều khối) → số dòng không đổi."""
    for _ in range(3):
        _load_sales(bronze, token="dt=2026-09-23/a.parquet#sha256-aaa")
    assert bronze.count("bronze_sale") == 1000


def test_same_token_with_new_content_is_silently_dropped(bronze: Db) -> None:
    """Quy tắc 1: token PHẢI chứa hash nội dung.

    Token chỉ là đường dẫn file → sửa file rồi nạp lại thì ClickHouse coi là trùng và BỎ bản
    sửa, không báo gì. Token có hash → bản sửa là một lần nạp mới (phần trùng lặp do đó phải
    xử lý bằng dựng lại phân vùng, quy tắc 3).
    """
    _load_sales(bronze, token="dt=2026-09-23/a.parquet")
    _load_sales(bronze, token="dt=2026-09-23/a.parquet", offset=5000)
    assert bronze.count("bronze_sale") == 1000, "bản sửa lẽ ra bị bỏ — hành vi ClickHouse đã đổi?"

    _load_sales(bronze, token="dt=2026-09-23/a.parquet#sha256-bbb", offset=5000)
    assert bronze.count("bronze_sale") == 2000


def test_dedup_window_is_finite(bronze: Db) -> None:
    """Quy tắc 2: nạp lại file CŨ HƠN cửa sổ khử trùng → nhân đôi.

    Bronze để cửa sổ 100 000 khối. Test dùng cửa sổ 3 để thấy giới hạn mà không phải chèn
    100 000 lần. Chạy lại quá xa thì phải đi đường dựng lại phân vùng.
    """
    bronze.query(
        "CREATE TABLE w3 (x UInt64) ENGINE = MergeTree ORDER BY x"
        " SETTINGS non_replicated_deduplication_window = 3"
    )
    bronze.query("INSERT INTO w3 VALUES (1)", insert_deduplication_token="old")
    for i in range(4):
        bronze.query(f"INSERT INTO w3 VALUES ({i + 10})", insert_deduplication_token=f"n{i}")  # noqa: S608
    bronze.query("INSERT INTO w3 VALUES (1)", insert_deduplication_token="old")
    assert int(bronze.query("SELECT countIf(x = 1) FROM w3")) == 2


def test_rebuild_by_dropping_the_partition_reloads_cleanly(bronze: Db) -> None:
    """Quy tắc 3: đường dựng lại. Sau DROP PARTITION, nạp lại cùng token vẫn vào được.

    Nếu khử trùng chặn cả lần nạp lại này, "xóa warehouse, dựng lại từ bronze" (cổng A) sẽ
    âm thầm để lại một phân vùng RỖNG.
    """
    token = "dt=2026-09-23/a.parquet#sha256-aaa"
    _load_sales(bronze, token=token)
    bronze.query("ALTER TABLE bronze_sale DROP PARTITION 202609")
    assert bronze.count("bronze_sale") == 0

    _load_sales(bronze, token=token)
    _load_sales(bronze, token=token)
    assert bronze.count("bronze_sale") == 1000


def test_no_pii_columns_in_bronze(bronze: Db) -> None:
    """Ràng buộc #10 — SĐT/tên không vào warehouse dưới dạng nào, kể cả hash."""
    columns: Any = bronze.query(
        "SELECT name FROM system.columns WHERE database = currentDatabase() FORMAT TSV"
    ).splitlines()
    leaked = [c for c in columns if re.search(r"phone|name_enc|variance_note", c)]
    assert leaked == []


def test_content_hash_dedups_deterministic_blocks_but_only_token_saves_default_now(
    bronze: Db,
) -> None:
    """Vì sao vẫn cần token dù ClickHouse đã khử trùng theo hash nội dung khối.

    Có cửa sổ khử trùng thì ClickHouse tự khử trùng theo hash của khối: nạp lại cùng file vào
    `bronze_sale` KHÔNG nhân đôi kể cả không có token (kiểm đột biến 2026-09-24 cho thấy đúng
    vậy). Nhưng `bronze_load_log.loaded_at DEFAULT now64()` làm nội dung khối đổi mỗi lần ghi:
    hash khác → không khử trùng → dòng nhật ký nhân đôi. Chỉ token (danh tính của lần nạp,
    không phụ thuộc nội dung) chặn được. Token là thứ luôn đúng; hash nội dung chỉ đúng khi
    nội dung tất định.
    """
    row = "('sale', 'a.parquet', 'aaa', 10, NULL, NULL)"
    cols = "(table_name, path, sha256, rows, window_start, window_end)"
    for _ in range(2):
        bronze.query(f"INSERT INTO bronze_load_log {cols} VALUES {row}")  # noqa: S608
    assert bronze.count("bronze_load_log") == 2, "hash nội dung lẽ ra khác nhau vì now64()"

    bronze.query("TRUNCATE TABLE bronze_load_log")
    for _ in range(2):
        bronze.query(
            f"INSERT INTO bronze_load_log {cols} VALUES {row}",  # noqa: S608
            insert_deduplication_token="a.parquet#aaa",
        )
    assert bronze.count("bronze_load_log") == 1
