"""Bảo trì định kỳ của trung tâm — service `central-maintenance` (compose).

Hai việc phải chạy theo lịch mà không ai được quên, gộp vào MỘT tiến trình:

1. **Tạo trước partition `point_ledger`** (ràng buộc #2, docs/08 §4.1) — `central.ops.partitions`.
   Tới 2026-09-25 hàm này không được gọi ở đâu ngoài migration đầu: docstring của nó ghi "gọi từ
   Airflow" nhưng DAG chưa bao giờ gọi. Migration 17/09 tạo sẵn 3 tháng, nên hệ thống sẽ chạy tốt
   tới đúng 00:00 ngày đầu tháng không còn partition — rồi MỌI sự kiện có điểm của mọi cửa hàng bị
   từ chối (`integrity_violation: 23514`, vào dead-letter). Đúng loại lỗi "im lặng tới phút cuối".
2. **Đối soát INV-4 tăng dần** (ràng buộc #8, DI-1) — `central.ops.reconcile`. Tới hạn
   (`RECONCILE_FULL_EVERY_DAYS`, mặc định 30) và đang giờ thấp điểm (01:00–05:00 giờ cửa hàng)
   thì lượt đó quét TOÀN BỘ: lưới bắt snapshot hỏng của khách không còn giao dịch — thứ lượt tăng
   dần cố ý bỏ qua. Lệnh `--full` có từ đầu nhưng, giống hàm tạo partition, không ai lên lịch.

Partition chạy TRƯỚC, trong transaction riêng và ngắn (DDL chỉ khi thiếu tháng): đối soát lỗi
không được kéo việc tạo partition chết theo. Mỗi lượt lỗi chỉ làm lỡ lượt đó, không làm chết lịch.
Chạy mỗi giờ là thừa cho partition (cần mỗi ngày) nhưng rẻ: không thiếu tháng thì không có DDL.

    uv run python -m central.ops.maintenance              # một lượt
    uv run python -m central.ops.maintenance --every 3600 # theo lịch (service compose)

Vùng đệm partition còn thấy trên dashboard: `point_ledger_partition_months_ahead` (bộ giám sát
luồng đọc view `point_ledger_partition_health`), cảnh báo khi < 2 tháng.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import text

from central.ops.partitions import PartitionHealth, run_daily_maintenance
from central.ops.reconcile import FULL_JOB_NAME, ReconcileReport, reconcile_points
from shared.db import transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from shared.config import ReturnRules

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FullScanPolicy:
    """Khi nào lượt đối soát là quét toàn bộ. `every_days <= 0` = không bao giờ tự quét."""

    every_days: float = 30.0
    start_hour: int = 1
    end_hour: int = 5
    utc_offset_minutes: int = 420

    def off_peak(self, now: datetime) -> bool:
        local = now + timedelta(minutes=self.utc_offset_minutes)
        return self.start_hour <= local.hour < self.end_hour

    def due(self, last_full: datetime | None, now: datetime) -> bool:
        if self.every_days <= 0 or not self.off_peak(now):
            return False
        return last_full is None or now - last_full >= timedelta(days=self.every_days)


_LAST_FULL = text("SELECT last_run_at FROM reconciliation_watermark WHERE job_name = :job")


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    partitions: PartitionHealth | None
    reconcile: ReconcileReport | None
    errors: dict[str, str]


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    rules: ReturnRules,
    months_ahead: int,
    full_scan: FullScanPolicy | None = None,
    now: datetime | None = None,
) -> MaintenanceReport:
    """Một lượt: partition rồi đối soát. Không bao giờ ném lỗi — lỗi nằm trong `errors`.

    `full_scan` quyết lượt đối soát này là tăng dần hay toàn bộ (mặc định: không bao giờ toàn bộ
    — giữ hành vi cho người gọi cũ và test)."""
    errors: dict[str, str] = {}
    health: PartitionHealth | None = None
    report: ReconcileReport | None = None
    try:
        async with transaction(session_factory) as session:
            health = await run_daily_maintenance(session, months_ahead=months_ahead)
    except Exception as exc:
        log.exception("maintenance: tạo partition lỗi")
        errors["partitions"] = f"{type(exc).__name__}: {exc}"
    try:
        full = False
        if full_scan is not None:
            async with session_factory() as session:
                last = (await session.execute(_LAST_FULL, {"job": FULL_JOB_NAME})).scalar()
            full = full_scan.due(last, now or datetime.now(UTC))
        report = await reconcile_points(session_factory, rules=rules, full=full)
    except Exception as exc:
        log.exception("maintenance: đối soát INV-4 lỗi")
        errors["reconcile"] = f"{type(exc).__name__}: {exc}"
    return MaintenanceReport(health, report, errors)


def describe(result: MaintenanceReport) -> str:
    parts: list[str] = []
    if (h := result.partitions) is not None:
        status = "OK" if h.is_healthy else "CẢNH BÁO"
        parts.append(
            f"partition[{status}] {h.partition_count} bảng, tới {h.last_partition_month},"
            f" đệm {h.months_ahead} tháng"
        )
    if (r := result.reconcile) is not None:
        kind = "full" if r.full else "incremental"
        parts.append(f"reconcile[{kind}] checked={r.checked_customers} drift={len(r.drifts)}")
    parts += [f"LỖI {k}: {v}" for k, v in result.errors.items()]
    return " · ".join(parts)


async def _main() -> int:  # pragma: no cover — điểm vào CLI
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--every", type=float, default=0.0, metavar="GIÂY", help="lặp mỗi N giây")
    args = parser.parse_args()

    from central.settings import get_return_rules, get_settings
    from shared.db import make_engine, make_session_factory

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    try:
        while True:
            result = await run_once(
                factory,
                rules=get_return_rules(),
                months_ahead=settings.point_ledger_partitions_ahead_months,
                full_scan=FullScanPolicy(
                    every_days=settings.reconcile_full_every_days,
                    start_hour=settings.reconcile_full_start_hour,
                    end_hour=settings.reconcile_full_end_hour,
                    utc_offset_minutes=settings.store_utc_offset_minutes,
                ),
            )
            print(describe(result), flush=True)
            if not args.every:
                unhealthy = result.partitions is None or not result.partitions.is_healthy
                drift = result.reconcile is None or bool(result.reconcile.drifts)
                return 1 if (result.errors or unhealthy or drift) else 0
            await asyncio.sleep(args.every)
    finally:
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    import sys

    from shared.console import utf8_stdio
    from shared.logs import setup_logging

    utf8_stdio()
    setup_logging("central-maintenance")
    sys.exit(asyncio.run(_main()))
