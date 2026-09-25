"""Mở ca, đóng ca — FR-P11, docs/14 §3.

Vì sao đây là Must của luồng dữ liệu, không phải tính năng để sau (ADR-010 quy tắc 2):
mỗi cửa hàng chỉ được có MỘT ca đang mở (`shift_one_open_per_store_idx`), và `business_date`
của mọi đơn lấy từ ca. Không đóng được ca thì cửa hàng giữ một ca mãi mãi, mọi đơn mang
`business_date` của ngày mở ca đầu tiên, và đối soát theo `(store_id, business_date)`
(docs/17 §5) mất hết ý nghĩa.

Phần đối chiếu tiền mặt (variance) vẫn là Could về NGHIỆP VỤ, nhưng nó chỉ là một phép trừ
trên số đã có — tính luôn ở đây rẻ hơn là để `ShiftClosed` thiếu trường rồi đổi hợp đồng sự
kiện sau.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from edge.pos.application.ports import OpenShift
from shared.events import ShiftClosedPayload
from shared.types import Money, new_event_id

if TYPE_CHECKING:
    from edge.pos.application.ports import EventPublisherPort, ShiftLifecyclePort

__all__ = [
    "CloseShiftCommand",
    "OpenShiftCommand",
    "ShiftCloseReport",
    "ShiftRejectedError",
    "close_shift",
    "open_shift",
]


class ShiftRejectedError(Exception):
    """Lỗi nghiệp vụ của mở/đóng ca. `code` được tầng route dịch sang HTTP.

    `shift_id` đi kèm `SHIFT_ALREADY_OPEN`: người gọi (thu ngân, bộ giả lập) cần biết ca nào
    đang mở để dùng tiếp hoặc đóng nó, thay vì phải đoán.
    """

    def __init__(self, code: str, message: str, *, shift_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.shift_id = shift_id


@dataclass(frozen=True, slots=True)
class OpenShiftCommand:
    store_id: str
    employee_id: str
    #: G5 — người mở ca khai TƯỜNG MINH, không suy từ `opened_at` (ca qua nửa đêm, B02 Z03).
    business_date: date
    opening_cash: Money
    opened_at: datetime
    store_utc_offset: timedelta


@dataclass(frozen=True, slots=True)
class CloseShiftCommand:
    shift_id: uuid.UUID
    store_id: str
    employee_id: str
    counted_cash: Money
    variance_note: str | None
    closed_at: datetime


@dataclass(frozen=True, slots=True)
class ShiftCloseReport:
    shift_id: uuid.UUID
    business_date: date
    opening_cash: Money
    cash_collected: Money
    expected_cash: Money
    counted_cash: Money
    variance: Money
    closed_at: datetime


def check_business_date(business_date: date, *, opened_at: datetime, offset: timedelta) -> None:
    """`business_date` phải là hôm nay hoặc hôm qua theo giờ cửa hàng.

    Hôm qua hợp lệ: ca đêm mở lúc 00:30 vẫn thuộc ngày kinh doanh trước (G5). Ngoài hai ngày
    đó gần như chắc chắn là đồng hồ hoặc client sai, và một `business_date` sai sẽ đi vào
    thẳng phân vùng warehouse — chặn ở đây rẻ hơn dọn ở đó.
    """
    local_today = (opened_at + offset).date()
    if business_date not in (local_today, local_today - timedelta(days=1)):
        raise ShiftRejectedError(
            "INVALID_BUSINESS_DATE",
            f"business_date {business_date} phải là {local_today} hoặc ngày trước đó",
        )


async def open_shift(command: OpenShiftCommand, *, shifts: ShiftLifecyclePort) -> OpenShift:
    if command.opening_cash < 0:
        raise ShiftRejectedError("INVALID_OPENING_CASH", "Tiền đầu ca không được âm")
    check_business_date(
        command.business_date, opened_at=command.opened_at, offset=command.store_utc_offset
    )

    shift_id = new_event_id()
    opened = await shifts.open(
        shift_id=shift_id,
        store_id=command.store_id,
        business_date=command.business_date,
        employee_id=command.employee_id,
        opening_cash=command.opening_cash,
        opened_at=command.opened_at,
    )
    if not opened:
        existing = await shifts.find_open_shift(command.store_id)
        raise ShiftRejectedError(
            "SHIFT_ALREADY_OPEN",
            "Cửa hàng đang có một ca mở — đóng ca đó trước",
            shift_id=existing.shift_id if existing else None,
        )
    return OpenShift(
        shift_id=shift_id, store_id=command.store_id, business_date=command.business_date
    )


async def close_shift(
    command: CloseShiftCommand, *, shifts: ShiftLifecyclePort, events: EventPublisherPort
) -> ShiftCloseReport:
    """Đóng ca: khóa ca → tính tiền mặt kỳ vọng → ghi → phát `ShiftClosed`, một transaction.

    Thứ tự khóa-rồi-mới-cộng là bắt buộc: đơn đang chốt dở giữ khóa chia sẻ trên ca
    (`get_open_shift` ... `FOR SHARE`), nên bước khóa ở đây ĐỢI mọi đơn đó commit rồi mới
    cộng tiền. Đảo thứ tự thì một đơn commit sau khi đã cộng sẽ nằm trong ca đã đóng mà
    không có mặt trong `expected_cash` — variance sai mà không ai biết vì sao.

    Không bao giờ sửa đơn nào để "cân" số liệu — variance là sự thật, ghi nguyên trạng (docs/14 §3).
    """
    if command.counted_cash < 0:
        raise ShiftRejectedError("INVALID_COUNTED_CASH", "Tiền đếm được không được âm")

    shift = await shifts.lock_open_for_close(command.shift_id, command.store_id)
    if shift is None:
        raise ShiftRejectedError(
            "SHIFT_CLOSED", f"Ca {command.shift_id} không mở hoặc không tồn tại"
        )

    cash = await shifts.cash_collected(shift.shift_id)
    expected = shift.opening_cash + cash
    variance = command.counted_cash - expected

    await shifts.mark_closed(
        shift_id=shift.shift_id,
        closed_by_employee_id=command.employee_id,
        closed_at=command.closed_at,
        expected_cash=expected,
        counted_cash=command.counted_cash,
        variance=variance,
        variance_note=command.variance_note,
    )
    await events.publish(
        event_type="ShiftClosed",
        occurred_at=command.closed_at,
        payload=ShiftClosedPayload(
            shift_id=shift.shift_id,
            business_date=shift.business_date,
            opened_by_employee_id=shift.opened_by_employee_id,
            closed_by_employee_id=command.employee_id,
            opened_at=shift.opened_at,
            closed_at=command.closed_at,
            opening_cash=shift.opening_cash,
            expected_cash=expected,
            counted_cash=command.counted_cash,
            variance=variance,
            variance_note=command.variance_note,
        ),
    )
    return ShiftCloseReport(
        shift_id=shift.shift_id,
        business_date=shift.business_date,
        opening_cash=shift.opening_cash,
        cash_collected=cash,
        expected_cash=expected,
        counted_cash=command.counted_cash,
        variance=variance,
        closed_at=command.closed_at,
    )
