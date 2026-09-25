"""Bảo trì partition `point_ledger` — ràng buộc #2, docs/08 §4.1.

Đây là rủi ro sập **cao nhất** của cả thiết kế, và nó có ba tính chất khó chịu:
  - Im lặng cho tới phút cuối: mọi thứ chạy tốt cho tới 00:00 ngày đầu tháng.
  - Bán kính ảnh hưởng là toàn hệ thống: mọi cửa hàng, mọi giao dịch có điểm.
  - Không tự phục hồi: cần người chạy DDL.

Vì vậy có hai hàm chứ không một: `ensure_partitions()` để *sửa*, và `check_health()` để
*biết trước khi phải sửa*. Chạy hằng ngày, cảnh báo khi vùng đệm tụt dưới ngưỡng.

Lịch: `central.ops.maintenance` (service compose `central-maintenance`, mỗi giờ). Thủ công:
    uv run python -m central.ops.partitions
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Cảnh báo khi vùng đệm còn dưới ngần này tháng. Thấp hơn `partitions_ahead_months` của
# settings để có thời gian phản ứng trước khi chạm đáy.
WARN_BELOW_MONTHS_AHEAD = 2


@dataclass(frozen=True, slots=True)
class PartitionHealth:
    partition_count: int
    last_partition_month: date | None
    months_ahead: int

    @property
    def is_healthy(self) -> bool:
        return self.months_ahead >= WARN_BELOW_MONTHS_AHEAD


async def ensure_partitions(session: AsyncSession, *, months_ahead: int = 3) -> int:
    """Tạo partition còn thiếu cho tháng hiện tại + `months_ahead` tháng tới.

    Idempotent: chạy lại không tạo trùng, trả về số partition thực sự được tạo. An toàn để
    gọi mỗi ngày — và nên gọi mỗi ngày, vì một lần lỡ là một tháng mất.
    """
    result = await session.execute(
        text("SELECT ensure_point_ledger_partitions(:months)"), {"months": months_ahead}
    )
    return int(result.scalar_one())


async def check_health(session: AsyncSession) -> PartitionHealth:
    """Đọc view `point_ledger_partition_health`. Rẻ — quét catalog, không quét dữ liệu."""
    row = (
        await session.execute(
            text(
                """
                SELECT partition_count, last_partition_month, months_ahead
                FROM point_ledger_partition_health
                """
            )
        )
    ).one()
    return PartitionHealth(
        partition_count=row.partition_count,
        last_partition_month=row.last_partition_month,
        months_ahead=row.months_ahead,
    )


async def run_daily_maintenance(session: AsyncSession, *, months_ahead: int = 3) -> PartitionHealth:
    """Tạo trước rồi kiểm sau.

    Thứ tự này có chủ đích: nếu `ensure` thất bại (hết quyền, hết đĩa), `check` sau đó sẽ
    báo vùng đệm thấp và cảnh báo nổ. Kiểm trước thì lỗi của `ensure` đi vào im lặng.
    """
    await ensure_partitions(session, months_ahead=months_ahead)
    return await check_health(session)


async def _main() -> int:  # pragma: no cover — điểm vào CLI
    from central.settings import get_settings
    from shared.db import make_engine, make_session_factory, transaction

    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        async with transaction(make_session_factory(engine)) as session:
            health = await run_daily_maintenance(
                session, months_ahead=settings.point_ledger_partitions_ahead_months
            )
    finally:
        await engine.dispose()

    status = "OK" if health.is_healthy else "CẢNH BÁO"
    print(
        f"[{status}] point_ledger: {health.partition_count} partition, "
        f"tới {health.last_partition_month}, còn {health.months_ahead} tháng đệm"
    )
    return 0 if health.is_healthy else 1


if __name__ == "__main__":  # pragma: no cover
    import asyncio
    import sys

    sys.exit(asyncio.run(_main()))
