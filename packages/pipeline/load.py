"""S5 — nạp file bronze từ lake vào `bronze_*` ở ClickHouse (docs/17 §2, §4 bẫy 4).

Ba quy tắc rút ra từ test trên ClickHouse thật (`test_clickhouse_bronze.py`), không từ tài liệu:
  1. token = đường dẫn + SHA-256 nội dung;
  2. cửa sổ khử trùng có hạn → dựng lại thì `DROP PARTITION` rồi nạp lại;
  3. mọi bảng bronze khai `non_replicated_deduplication_window` (DDL).

Đọc file bằng MỘT luồng (`max_threads=1`) để khối insert được tách giống hệt nhau giữa hai
lần chạy: token gắn hậu tố số thứ tự khối, nên khối tách khác đi là khử trùng vô dụng.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyarrow.parquet as pq

from pipeline.clickhouse import quote
from pipeline.extract import dt_of, parse_window

if TYPE_CHECKING:
    from pipeline.clickhouse import ClickHouse
    from pipeline.lake import Lake
    from pipeline.tables import TableSpec

__all__ = ["Loaded", "load_file", "load_pending", "loaded_paths"]

#: Cài đặt đọc/ghi tất định cho một lần nạp.
DETERMINISTIC = {"max_threads": 1, "max_insert_threads": 1, "insert_deduplicate": 1}


@dataclass(frozen=True, slots=True)
class Loaded:
    table: str
    key: str
    rows: int


def token(key: str, sha256: str) -> str:
    return f"{key}#{sha256}"


def loaded_paths(ch: ClickHouse, spec: TableSpec) -> set[str]:
    return {
        r[0]
        for r in ch.rows(
            f"SELECT DISTINCT path FROM bronze_load_log WHERE table_name = {quote(spec.name)}"
        )
    }


def load_file(ch: ClickHouse, lake: Lake, spec: TableSpec, key: str) -> Loaded:
    data = lake.get(key)
    sha = hashlib.sha256(data).hexdigest()
    rows = pq.read_metadata(io.BytesIO(data)).num_rows
    tok = token(key, sha)
    dt = dt_of(key)

    names = [c.name for c in spec.columns]
    exprs = [c.ch_expr() for c in spec.columns]
    source = (
        f"s3({quote(lake.url_for_clickhouse(key))}, {quote(lake.cfg.access_key)},"
        f" {quote(lake.cfg.secret_key)}, 'Parquet')"
    )
    ch.query(
        f"INSERT INTO {spec.bronze} ({', '.join(names)}, _dt, _source_file)"
        f" SELECT {', '.join(exprs)}, toDate({quote(dt)}), {quote(key)} FROM {source}",
        insert_deduplication_token=tok,
        **DETERMINISTIC,
    )
    window = parse_window(key)
    start = quote(window.start.strftime("%Y-%m-%d %H:%M:%S")) if window else "NULL"
    end = quote(window.end.strftime("%Y-%m-%d %H:%M:%S")) if window else "NULL"
    ch.query(
        "INSERT INTO bronze_load_log (table_name, path, sha256, rows, window_start, window_end)"
        f" VALUES ({quote(spec.name)}, {quote(key)}, {quote(sha)}, {rows}, {start}, {end})",
        insert_deduplication_token=tok,
    )
    return Loaded(spec.name, key, rows)


def load_pending(ch: ClickHouse, lake: Lake, spec: TableSpec) -> list[Loaded]:
    """Nạp mọi file có trong lake mà nhật ký nạp chưa ghi — theo thứ tự thời gian."""
    done = loaded_paths(ch, spec)
    return [load_file(ch, lake, spec, key) for key in lake.list_table(spec.name) if key not in done]
