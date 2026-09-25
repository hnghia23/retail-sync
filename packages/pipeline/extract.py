"""S4 — trích xuất Postgres TRUNG TÂM → Parquet trên lake (docs/17 §2, §4).

## Cửa sổ

Cửa sổ nối tiếp nhau: cửa sổ đầu bắt đầu từ `recorded_at` nhỏ nhất (làm tròn xuống theo độ
dài cửa sổ), mỗi cửa sổ sau bắt đầu ĐÚNG ở mép cuối của file cuối cùng đã có trong lake. Vì
vậy đổi độ dài cửa sổ giữa chừng không tạo ra chồng lấn, và không cần bảng trạng thái nào.

Chỉ trích một cửa sổ khi mép cuối ≤ `extract_horizon()`: mọi transaction có thể ghi dòng vào
cửa sổ đó đều đã kết thúc (bẫy 1). Dòng tương lai luôn mang `recorded_at` ≥ mốc hiện tại, nên
một cửa sổ đã trích là ĐỦ vĩnh viễn — lý do file được phép bất biến.

## Tất định

Cùng dữ liệu → cùng thứ tự dòng (`ORDER BY` trong đặc tả) → cùng nhóm dòng Parquet → cùng
bytes → cùng SHA-256 → cùng token khử trùng ở ClickHouse (bẫy 4).
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    import asyncpg

    from pipeline.lake import Lake
    from pipeline.tables import TableSpec

__all__ = [
    "Extracted",
    "Window",
    "dt_of",
    "extract_horizon",
    "extract_incremental",
    "extract_snapshot",
    "parse_window",
]

#: Dòng mỗi nhóm Parquet (row group) và mỗi lần kéo từ cursor. Cố định để bytes tất định.
BATCH_ROWS = 50_000
_STAMP = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime

    def file_name(self) -> str:
        return f"{self.start:{_STAMP}}_{self.end:{_STAMP}}.parquet"


@dataclass(frozen=True, slots=True)
class Extracted:
    table: str
    key: str
    sha256: str
    rows: int
    dt: str
    window: Window | None
    #: False = file đã có từ trước (chạy lại) — KHÔNG ghi lại.
    created: bool


def parse_window(key: str) -> Window | None:
    name = key.rsplit("/", 1)[-1].removesuffix(".parquet")
    start, sep, end = name.partition("_")
    if not sep:
        return None
    try:
        return Window(
            datetime.strptime(start, _STAMP).replace(tzinfo=UTC),
            datetime.strptime(end, _STAMP).replace(tzinfo=UTC),
        )
    except ValueError:
        return None


def dt_of(key: str) -> str:
    part = next(p for p in key.split("/") if p.startswith("dt="))
    return part.removeprefix("dt=")


async def extract_horizon(conn: asyncpg.Connection, *, safety_lag_seconds: float) -> datetime:
    value: datetime = await conn.fetchval(
        "SELECT extract_horizon(make_interval(secs => $1))", safety_lag_seconds
    )
    return value


def _floor(ts: datetime, seconds: int) -> datetime:
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)


async def _to_parquet(conn: asyncpg.Connection, spec: TableSpec, *args: Any) -> tuple[bytes, int]:
    schema = spec.schema()
    buf = io.BytesIO()
    rows = 0
    with pq.ParquetWriter(buf, schema, compression="zstd") as writer:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            cursor = await conn.cursor(spec.select_sql(), *args)
            while True:
                batch = await cursor.fetch(BATCH_ROWS)
                if not batch:
                    break
                columns = {c.name: [r[c.name] for r in batch] for c in spec.columns}
                writer.write_table(pa.table(columns, schema=schema))
                rows += len(batch)
    return buf.getvalue(), rows


async def extract_incremental(
    conn: asyncpg.Connection,
    lake: Lake,
    spec: TableSpec,
    *,
    horizon: datetime,
    window_seconds: int,
    max_windows: int | None = None,
) -> list[Extracted]:
    """Trích MỌI cửa sổ còn thiếu và đã an toàn (mép cuối ≤ `horizon`), theo thứ tự."""
    if spec.kind != "incremental":
        raise ValueError(f"{spec.name} không phải bảng incremental")

    step = timedelta(seconds=window_seconds)
    ends = [w.end for k in lake.list_table(spec.name) if (w := parse_window(k))]
    if ends:
        start = max(ends)
    else:
        first = await conn.fetchval(f"SELECT min({spec.watermark}) FROM {spec.source}")
        if first is None:
            # Bảng rỗng vẫn ghi MỘT file rỗng làm mốc, ngay trước `floor(horizon)`. Không có
            # file thì bảng vắng khỏi nhật ký nạp, và mép "đã nạp tới" (min trên mọi bảng) của
            # dbt lẫn audit L3 không tính được: hoặc tắc vĩnh viễn, hoặc phải bỏ qua bảng thiếu
            # — mà bỏ qua thì lần nạp đầu chết giữa chừng làm fact thiếu dòng mãi mãi.
            # Đúng vì dòng tương lai luôn có recorded_at ≥ horizon (docs/17 §4 bẫy 1).
            start = _floor(horizon, window_seconds) - step
        else:
            start = _floor(first, window_seconds)

    out: list[Extracted] = []
    while start + step <= horizon and (max_windows is None or len(out) < max_windows):
        window = Window(start, start + step)
        dt = window.start.date().isoformat()
        key = lake.key(spec.name, dt, window.file_name())
        if lake.exists(key):  # chạy lại / tiến trình khác đã làm — bất biến, không ghi đè
            out.append(Extracted(spec.name, key, lake.sha256(key), -1, dt, window, False))
        else:
            data, rows = await _to_parquet(conn, spec, window.start, window.end)
            lake.put(key, data)
            digest = hashlib.sha256(data).hexdigest()
            out.append(Extracted(spec.name, key, digest, rows, dt, window, True))
        start = window.end
    return out


async def extract_snapshot(
    conn: asyncpg.Connection, lake: Lake, spec: TableSpec, *, day: date
) -> Extracted:
    """Một file mỗi ngày. Đã có file của ngày đó → giữ nguyên (bất biến trong ngày)."""
    if spec.kind != "snapshot":
        raise ValueError(f"{spec.name} không phải bảng snapshot")
    dt = day.isoformat()
    key = lake.key(spec.name, dt, "snapshot.parquet")
    if lake.exists(key):
        return Extracted(spec.name, key, lake.sha256(key), -1, dt, None, False)
    data, rows = await _to_parquet(conn, spec)
    lake.put(key, data)
    return Extracted(spec.name, key, hashlib.sha256(data).hexdigest(), rows, dt, None, True)
