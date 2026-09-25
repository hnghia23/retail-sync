"""Use case mở/đóng ca bằng fake — không cần Docker. Race khóa ca kiểm ở test tích hợp."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from edge.pos.application.ports import OpenShift, ShiftToClose
from edge.pos.application.shifts import (
    CloseShiftCommand,
    OpenShiftCommand,
    ShiftRejectedError,
    check_business_date,
    close_shift,
    open_shift,
)
from shared.events import ShiftClosedPayload

VN = timedelta(hours=7)


@dataclass
class FakeShifts:
    open_rows: dict[str, OpenShift] = field(default_factory=dict)
    to_close: ShiftToClose | None = None
    cash: int = 0
    closed: dict[str, Any] = field(default_factory=dict)

    async def open(self, **kw: Any) -> bool:
        if kw["store_id"] in self.open_rows:
            return False
        self.open_rows[kw["store_id"]] = OpenShift(
            shift_id=kw["shift_id"], store_id=kw["store_id"], business_date=kw["business_date"]
        )
        return True

    async def find_open_shift(self, store_id: str) -> OpenShift | None:
        return self.open_rows.get(store_id)

    async def lock_open_for_close(self, shift_id: uuid.UUID, store_id: str) -> ShiftToClose | None:
        if self.to_close and self.to_close.shift_id == shift_id and not self.closed:
            return self.to_close
        return None

    async def cash_collected(self, shift_id: uuid.UUID) -> int:
        return self.cash

    async def mark_closed(self, **kw: Any) -> None:
        self.closed = kw


@dataclass
class FakeEvents:
    published: list[tuple[str, Any]] = field(default_factory=list)

    async def publish(self, *, event_type: str, payload: Any, **_: Any) -> uuid.UUID:
        self.published.append((event_type, payload))
        return uuid.uuid4()


def _open_cmd(business_date: date, opened_at: datetime) -> OpenShiftCommand:
    return OpenShiftCommand(
        store_id="store-001",
        employee_id="e1",
        business_date=business_date,
        opening_cash=500_000,
        opened_at=opened_at,
        store_utc_offset=VN,
    )


# ═══════════════ business_date — G5 ═══════════════


def test_business_date_uses_store_time_not_utc() -> None:
    """20:00 UTC ngày 23 = 03:00 sáng ngày 24 ở Việt Nam."""
    at = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
    check_business_date(date(2026, 9, 24), opened_at=at, offset=VN)
    check_business_date(date(2026, 9, 23), opened_at=at, offset=VN)  # ca đêm, ngày trước


@pytest.mark.parametrize("business_date", [date(2026, 9, 22), date(2026, 9, 25)])
def test_business_date_outside_today_or_yesterday_is_rejected(business_date: date) -> None:
    at = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)  # 24/09 giờ VN
    with pytest.raises(ShiftRejectedError) as exc:
        check_business_date(business_date, opened_at=at, offset=VN)
    assert exc.value.code == "INVALID_BUSINESS_DATE"


# ═══════════════ Mở ca ═══════════════


async def test_second_open_reports_the_shift_that_is_already_open() -> None:
    shifts = FakeShifts()
    at = datetime(2026, 9, 23, 1, 0, tzinfo=UTC)
    first = await open_shift(_open_cmd(date(2026, 9, 23), at), shifts=shifts)

    with pytest.raises(ShiftRejectedError) as exc:
        await open_shift(_open_cmd(date(2026, 9, 23), at), shifts=shifts)
    assert exc.value.code == "SHIFT_ALREADY_OPEN"
    assert exc.value.shift_id == first.shift_id


# ═══════════════ Đóng ca ═══════════════


async def test_close_computes_expected_cash_and_publishes_shift_closed() -> None:
    shift_id = uuid.uuid4()
    opened_at = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
    closed_at = opened_at + timedelta(hours=8)
    shifts = FakeShifts(
        to_close=ShiftToClose(shift_id, "store-001", date(2026, 9, 23), "e1", opened_at, 500_000),
        cash=1_620_000,
    )
    events = FakeEvents()

    report = await close_shift(
        CloseShiftCommand(shift_id, "store-001", "m1", 2_100_000, "thiếu 20k", closed_at),
        shifts=shifts,
        events=events,
    )

    assert report.expected_cash == 2_120_000
    assert report.variance == -20_000  # ghi nhận nguyên trạng, không "cân" về 0
    assert shifts.closed["variance"] == -20_000

    [(event_type, payload)] = events.published
    assert event_type == "ShiftClosed"
    assert isinstance(payload, ShiftClosedPayload)
    assert payload.opened_by_employee_id == "e1"
    assert payload.closed_by_employee_id == "m1"
    assert payload.expected_cash == 2_120_000


async def test_closing_a_closed_shift_is_rejected_and_publishes_nothing() -> None:
    events = FakeEvents()
    with pytest.raises(ShiftRejectedError) as exc:
        await close_shift(
            CloseShiftCommand(uuid.uuid4(), "store-001", "m1", 0, None, datetime.now(UTC)),
            shifts=FakeShifts(),
            events=events,
        )
    assert exc.value.code == "SHIFT_CLOSED"
    assert events.published == []
