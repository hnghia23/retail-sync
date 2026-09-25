"""Bảo trì định kỳ của trung tâm — service `central-maintenance` (compose).

Hai việc phải chạy theo lịch mà không ai được quên, gộp vào MỘT tiến trình:

1. **Tạo trước partition `point_ledger`** (ràng buộc #2, docs/08 §4.1) — `central.ops.partitions`.
   Tới 2026-09-25 hàm này không được gọi ở đâu ngoài migration đầu: docstring của nó ghi "gọi từ
   Airflow" nhưng DAG chưa bao giờ gọi. Migration 17/09 tạo sẵn 3 tháng, nên hệ thống sẽ chạy tốt
   tới đúng 00:00 ngày đầu tháng không còn partition — rồi MỌI sự kiện có điểm của mọi cửa hàng bị
   từ chối (`integrity_violation: 23514`, vào dead-letter). Đúng loại lỗi "im lặng tới phút cuối".
2. **Đối soát INV-4 tăng dần** (ràng buộc #8, DI-1) — `central.ops.reconcile`.

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
from typing import TYPE_CHECKING

from central.ops.partitions import PartitionHealth, run_daily_maintenance
from central.ops.reconcile import ReconcileReport, reconcile_points
from shared.db import transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from shared.config import ReturnRules

log = logging.getLogger(__name__)


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
) -> MaintenanceReport:
    """Một lượt: partition rồi đối soát. Không bao giờ ném lỗi — lỗi nằm trong `errors`."""
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
        report = await reconcile_points(session_factory, rules=rules)
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
        parts.append(f"reconcile checked={r.checked_customers} drift={len(r.drifts)}")
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
