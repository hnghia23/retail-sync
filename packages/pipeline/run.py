"""Một lượt pipeline: trích xuất mọi cửa sổ đã an toàn, rồi nạp mọi file chưa nạp.

Chạy lại bao nhiêu lần cũng được (AT-07): file đã có không bị trích lại (lake bất biến),
file đã nạp bị bỏ qua (nhật ký nạp) — và kể cả khi nhật ký mất, token khử trùng vẫn chặn
nhân đôi trong cửa sổ khử trùng.

## Mỗi lúc chỉ MỘT lượt

"Kiểm tồn tại rồi ghi" trong `extract_incremental` không nguyên tử. Hai lượt chạy song song
(DAG Airflow và `make pipeline-run` từ máy dev, chẳng hạn) cùng trích một cửa sổ ở hai thời
điểm khác nhau: nếu giữa hai lần có một UPDATE chuyển dòng sang cửa sổ mới, lượt sau GHI ĐÈ
file bằng nội dung khác. Bronze giữ bản của lượt trước (đã ghi nhật ký nạp nên không nạp lại),
lake giữ bản của lượt sau, và dựng lại từ lake sẽ mất phiên bản cũ.

Vì vậy cả lượt (trích xuất + nạp) chạy dưới một advisory lock cấp PHIÊN trên Postgres trung
tâm. Lượt thứ hai không chờ mà dừng ngay (`PipelineBusyError`). Tiến trình chết thì kết nối
đóng và khóa tự nhả, không có khóa mồ côi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg

from pipeline.extract import Extracted, extract_horizon, extract_incremental, extract_snapshot
from pipeline.load import Loaded, load_pending
from pipeline.tables import INCREMENTAL, SNAPSHOT

if TYPE_CHECKING:
    from pipeline.clickhouse import ClickHouse
    from pipeline.config import PipelineConfig
    from pipeline.lake import Lake

__all__ = ["LOCK_KEY", "PipelineBusyError", "RunReport", "run_once"]

#: Khóa advisory của pipeline trên Postgres trung tâm (một số bigint cố định, "rspl").
LOCK_KEY = 0x7273706C


class PipelineBusyError(RuntimeError):
    """Một lượt pipeline khác đang giữ khóa. Không phải lỗi dữ liệu — thử lại lượt sau."""


@dataclass(slots=True)
class RunReport:
    horizon: datetime
    extracted: list[Extracted] = field(default_factory=list)
    loaded: list[Loaded] = field(default_factory=list)

    def summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for e in self.extracted:
            if e.created:
                t = out.setdefault(e.table, {"files": 0, "rows": 0, "loaded": 0})
                t["files"] += 1
                t["rows"] += e.rows
        for item in self.loaded:
            out.setdefault(item.table, {"files": 0, "rows": 0, "loaded": 0})["loaded"] += item.rows
        return out


async def run_once(
    cfg: PipelineConfig,
    lake: Lake,
    ch: ClickHouse,
    *,
    snapshot_day: datetime | None = None,
    utc_offset: timedelta = timedelta(hours=7),
) -> RunReport:
    conn = await asyncpg.connect(cfg.pg_dsn)
    try:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
            raise PipelineBusyError("Một lượt pipeline khác đang chạy (advisory lock đang bị giữ)")
        horizon = await extract_horizon(conn, safety_lag_seconds=cfg.safety_lag_seconds)
        report = RunReport(horizon=horizon)
        for spec in INCREMENTAL:
            report.extracted += await extract_incremental(
                conn, lake, spec, horizon=horizon, window_seconds=cfg.window_seconds
            )
        # Ngày của snapshot theo giờ cửa hàng (một bản mỗi ngày kinh doanh).
        day = ((snapshot_day or datetime.now(UTC)) + utc_offset).date()
        for spec in SNAPSHOT:
            report.extracted.append(await extract_snapshot(conn, lake, spec, day=day))
        # Nạp vẫn nằm TRONG khóa: kết nối chỉ đóng (và nhả khóa) sau khi nạp xong.
        for spec in (*INCREMENTAL, *SNAPSHOT):
            report.loaded += load_pending(ch, lake, spec)
    finally:
        await conn.close()
    return report
