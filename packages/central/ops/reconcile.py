"""Đối soát INV-4 — `point_balance` so với `point_ledger`. Ràng buộc #8, AT-10, DI-1.

Chạy:
    uv run python -m central.ops.reconcile           # tăng dần (hằng ngày / mỗi giờ)
    uv run python -m central.ops.reconcile --full    # quét toàn bộ (mỗi tháng, giờ thấp điểm)
    uv run python -m central.ops.reconcile --every 3600   # lặp (lịch thật: central.ops.maintenance)

Tới 2026-09-25 job chỉ chạy tay (`make reconcile`, test AT-10). Dashboard "sức khỏe luồng" lộ ra
điều đó ngay (`reconcile_last_run_age_seconds` = 16 giờ): "lệch = 0" của một job không chạy là
con số vô nghĩa. Hai lượt chồng nhau (lịch + chạy tay) vô hại: mọi lần ghi là upsert, và mốc bị
lùi chỉ làm lượt sau xét lại một cửa sổ.

## Tăng dần là bắt buộc, không phải tối ưu

Ở T3, `point_ledger` tăng 584 triệu dòng/năm, nên quét toàn bộ mỗi ngày là không khả thi
(docs/08 §4.2). Mỗi lần chạy chỉ xét:
  1. khách có dòng ledger mới trong cửa sổ `[mốc lần trước, extract_horizon())`, dùng index
     `point_ledger_recorded_idx`. Mép cửa sổ lấy từ `extract_horizon()` chứ không từ `now()`,
     cùng lý do với trích xuất bronze (docs/17 §4 bẫy 1): dòng commit muộn không được lọt;
  2. khách còn lệch CHƯA xử lý trong `reconciliation_drift`, để bản sửa được xác nhận.

Cái giá có chủ đích: snapshot của một khách bị hỏng mà khách đó không phát sinh giao dịch
nào nữa sẽ chỉ bị phát hiện ở lần `--full` kế tiếp. Test
`test_incremental_misses_untouched_customer_but_full_scan_finds_it` giữ đánh đổi này nhìn thấy được.

## Cùng định nghĩa với đường ghi

`lifetime_earned` tính lại bằng CHÍNH `central.ingest.handlers.lifetime_contribution` và
cùng phép chặn dưới `max(0, …)` theo thứ tự áp (`recorded_at`, `event_id`). Viết một định
nghĩa thứ hai ở đây thì job sẽ báo lệch khi hai định nghĩa lệch nhau, chứ không phải khi dữ
liệu lệch.

Job CHỈ BÁO, không tự sửa. Sửa sai hướng một snapshot điểm là sửa tiền của khách, và phải
có người nhìn vào trước.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import text

from central.ingest.handlers import lifetime_contribution
from shared.config import ReturnRules
from shared.db import transaction

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["JOB_NAME", "Drift", "ReconcileReport", "reconcile_points"]

JOB_NAME = "point_balance_vs_ledger"

#: Số khách mỗi transaction. Khách nhiều dòng ledger thì mỗi lô nặng hơn; 500 giữ một lô
#: dưới cỡ giây ở dữ liệu T2 mà vẫn ít round-trip.
BATCH = 500

_WATERMARK = text("SELECT last_event_at FROM reconciliation_watermark WHERE job_name = :job")
_HORIZON = text("SELECT extract_horizon(make_interval(secs => :lag))")

_CHANGED = text(
    """
    SELECT DISTINCT customer_id FROM point_ledger
    WHERE recorded_at >= :since AND recorded_at < :horizon
    """
)
_ALL = text(
    """
    SELECT customer_id FROM point_balance
    UNION SELECT DISTINCT customer_id FROM point_ledger WHERE recorded_at < :horizon
    """
)
_OPEN_DRIFTS = text(
    "SELECT DISTINCT customer_id FROM reconciliation_drift WHERE resolved_at IS NULL"
)

_LEDGER = text(
    """
    SELECT customer_id, delta, reason FROM point_ledger
    WHERE customer_id = ANY(:ids)
    ORDER BY customer_id, recorded_at, event_id
    """
)
_BALANCES = text(
    "SELECT customer_id, balance, lifetime_earned, tier FROM point_balance"
    " WHERE customer_id = ANY(:ids)"
)
_TIER_RULES = text("SELECT tier, min_lifetime_points FROM tier_rule ORDER BY min_lifetime_points")

_RECORD_DRIFT = text(
    """
    INSERT INTO reconciliation_drift (customer_id, field, expected, actual)
    VALUES (:customer_id, :field, :expected, :actual)
    ON CONFLICT (customer_id, field) DO UPDATE SET
        expected = EXCLUDED.expected, actual = EXCLUDED.actual,
        last_seen_at = now(), resolved_at = NULL
    """
)
_RESOLVE = text(
    """
    UPDATE reconciliation_drift SET resolved_at = now()
    WHERE customer_id = ANY(:ids) AND resolved_at IS NULL
      AND NOT ((customer_id, field) IN (SELECT * FROM unnest(CAST(:drift_ids AS uuid[]),
                                                            CAST(:drift_fields AS text[]))))
    """
)
_ADVANCE = text(
    """
    INSERT INTO reconciliation_watermark (job_name, last_event_at, last_run_at, mismatch_count)
    VALUES (:job, :horizon, now(), :mismatches)
    ON CONFLICT (job_name) DO UPDATE SET
        last_event_at = EXCLUDED.last_event_at, last_run_at = now(),
        mismatch_count = EXCLUDED.mismatch_count
    """
)


@dataclass(frozen=True, slots=True)
class Drift:
    """Một lệch. KHÔNG mang PII — chỉ `customer_id` (ràng buộc #10), an toàn để log."""

    customer_id: uuid.UUID
    field: str
    expected: str
    actual: str | None


@dataclass(slots=True)
class ReconcileReport:
    window_start: datetime | None
    window_end: datetime
    full: bool
    checked_customers: int = 0
    drifts: list[Drift] = field(default_factory=list)


def _tier_for(lifetime: int, rules: list[tuple[str, int]]) -> str:
    tier = "BRONZE"
    for name, minimum in rules:
        if minimum <= lifetime:
            tier = name
    return tier


def _expected(rows: list[tuple[int, str]], rules: ReturnRules) -> tuple[int, int]:
    """(balance, lifetime_earned) suy từ ledger — theo đúng cách đường ghi cộng dồn."""
    balance = 0
    lifetime = 0
    for delta, reason in rows:
        balance += delta
        lifetime = max(0, lifetime + lifetime_contribution(delta, reason, rules))
    return balance, lifetime


async def _check_batch(
    session: AsyncSession, ids: list[uuid.UUID], *, rules: ReturnRules
) -> list[Drift]:
    # Ledger và snapshot phải đọc trong CÙNG một snapshot: ingest ghi hai bảng trong một
    # SAVEPOINT, nên REPEATABLE READ thấy cả hai hoặc không thấy cả hai — không có "lệch
    # giả" do đọc giữa chừng một lô đang commit.
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    ledger: dict[uuid.UUID, list[tuple[int, str]]] = defaultdict(list)
    for row in await session.execute(_LEDGER, {"ids": ids}):
        ledger[row.customer_id].append((row.delta, row.reason))
    balances = {row.customer_id: row for row in await session.execute(_BALANCES, {"ids": ids})}
    tier_rules = [(r.tier, r.min_lifetime_points) for r in await session.execute(_TIER_RULES)]

    drifts: list[Drift] = []
    for customer_id in ids:
        rows = ledger.get(customer_id, [])
        snapshot = balances.get(customer_id)
        if snapshot is None:
            if rows:
                drifts.append(Drift(customer_id, "missing", str(len(rows)), None))
            continue
        balance, lifetime = _expected(rows, rules)
        tier = _tier_for(lifetime, tier_rules)
        for name, want, got in (
            ("balance", balance, snapshot.balance),
            ("lifetime_earned", lifetime, snapshot.lifetime_earned),
            ("tier", tier, snapshot.tier),
        ):
            if want != got:
                drifts.append(Drift(customer_id, name, str(want), str(got)))
    return drifts


async def reconcile_points(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    rules: ReturnRules,
    full: bool = False,
    safety_lag_seconds: float = 30.0,
) -> ReconcileReport:
    async with transaction(session_factory) as session:
        since = (await session.execute(_WATERMARK, {"job": JOB_NAME})).scalar_one_or_none()
        horizon = (await session.execute(_HORIZON, {"lag": safety_lag_seconds})).scalar_one()
        if full or since is None:
            candidates = {r[0] for r in await session.execute(_ALL, {"horizon": horizon})}
        else:
            candidates = {
                r[0] for r in await session.execute(_CHANGED, {"since": since, "horizon": horizon})
            }
        candidates |= {r[0] for r in await session.execute(_OPEN_DRIFTS)}

    scanned_all = full or since is None  # lần chạy đầu tiên chưa có mốc = quét toàn bộ
    report = ReconcileReport(
        window_start=None if scanned_all else since, window_end=horizon, full=scanned_all
    )
    ordered = sorted(candidates)
    for i in range(0, len(ordered), BATCH):
        ids = ordered[i : i + BATCH]
        async with transaction(session_factory) as session:
            drifts = await _check_batch(session, ids, rules=rules)
        async with transaction(session_factory) as session:
            for d in drifts:
                await session.execute(
                    _RECORD_DRIFT,
                    {
                        "customer_id": d.customer_id,
                        "field": d.field,
                        "expected": d.expected,
                        "actual": d.actual,
                    },
                )
            await session.execute(
                _RESOLVE,
                {
                    "ids": ids,
                    "drift_ids": [d.customer_id for d in drifts],
                    "drift_fields": [d.field for d in drifts],
                },
            )
        report.checked_customers += len(ids)
        report.drifts.extend(drifts)

    async with transaction(session_factory) as session:
        await session.execute(
            _ADVANCE, {"job": JOB_NAME, "horizon": horizon, "mismatches": len(report.drifts)}
        )
    return report


async def _main() -> None:  # pragma: no cover — điểm vào CLI
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="quét toàn bộ thay vì tăng dần")
    parser.add_argument(
        "--every",
        type=float,
        default=0.0,
        metavar="GIÂY",
        help="lặp lại mỗi N giây; có lệch vẫn chạy tiếp (lệch nằm ở DB + dashboard)",
    )
    args = parser.parse_args()

    from central.settings import get_return_rules, get_settings
    from shared.db import make_engine, make_session_factory

    engine = make_engine(get_settings().database_url)
    factory = make_session_factory(engine)
    try:
        while True:
            try:
                report = await reconcile_points(factory, rules=get_return_rules(), full=args.full)
            except Exception as exc:
                if not args.every:
                    raise
                # Chạy theo lịch: DB trung tâm tạm chết chỉ làm lỡ một lượt, không làm chết lịch.
                print(f"reconcile: lỗi {type(exc).__name__}: {exc} — thử lại lượt sau", flush=True)
            else:
                _print(report)
                if report.drifts and not args.every:
                    raise SystemExit(1)  # mã thoát khác 0: Airflow/cron đánh dấu task đỏ
            if not args.every:
                return
            await asyncio.sleep(args.every)
    finally:
        await engine.dispose()


def _print(report: ReconcileReport) -> None:
    print(
        f"reconcile full={report.full} window=[{report.window_start}, {report.window_end})"
        f" checked={report.checked_customers} drift={len(report.drifts)}",
        flush=True,
    )
    for d in report.drifts[:50]:
        print(
            f"  DRIFT customer={d.customer_id} {d.field}: ledger={d.expected} snapshot={d.actual}"
        )


if __name__ == "__main__":  # pragma: no cover
    from shared.console import utf8_stdio

    utf8_stdio()
    asyncio.run(_main())
