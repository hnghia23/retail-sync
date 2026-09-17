"""Hợp đồng sự kiện outbox — hiện thực hóa docs/12-event-schema.md.

Đây là **ranh giới** giữa cửa hàng và trung tâm. Lý do viết trước use case: v1 chết đúng ở
4 điểm nối (docs/09-v1-postmortem.md). Cả hai đầu import CÙNG module này, nên không thể
lệch nhau âm thầm.

Ghi nhớ:
  - `event_id` UUIDv7 sinh tại cửa hàng = khóa idempotency ở cả hai đầu.
  - `recorded_at` KHÔNG nằm trong envelope — trung tâm tự gán `now()` khi nhận.
  - Không PII trong payload: chỉ `customer_id`, và phone/name ở dạng hash/mã hóa.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.types import Money, PaymentMethod, PointReason

SCHEMA_VERSION = 1

EventType = Literal[
    "SaleCompleted",
    "SaleReturned",
    "PointsEarned",
    "PointsReturned",
    "PointsAdjusted",
    "CustomerCreated",
    "CustomerUpdated",
    "ShiftClosed",
]


class _Strict(BaseModel):
    """Payload phía GỬI: cấm field lạ để lỗi chính tả không lọt ra khỏi cửa hàng."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _Lenient(BaseModel):
    """Envelope phía NHẬN: cho phép field lạ.

    docs/12 §4: thêm field optional KHÔNG tăng `schema_version` — consumer cũ phải bỏ qua
    field lạ thay vì nổ. Đây là quy tắc tiến hóa, không phải sự lỏng lẻo.
    """

    model_config = ConfigDict(extra="allow")


# ═══════════════════════════ Payload từng loại ═══════════════════════════


class SaleLinePayload(_Strict):
    line_no: int
    product_id: str
    quantity: int = Field(description="Âm khi trả hàng")
    unit_price: Money = 0
    line_total: Money = 0
    # G3 — trả hàng một phần: trỏ ngược về dòng gốc
    original_sale_id: uuid.UUID | None = None
    original_sale_line_no: int | None = None


class SalePaymentPayload(_Strict):
    seq: int
    method: PaymentMethod
    amount: Money
    reference: str | None = None


class SaleCompletedPayload(_Strict):
    """docs/12 §3.1. `lines` + `payments` đi CÙNG sự kiện, không tách 3 sự kiện con —
    trung tâm ghi cả 3 bảng replica từ một lần dedupe, tránh race."""

    sale_id: uuid.UUID
    shift_id: uuid.UUID
    employee_id: str
    customer_id: uuid.UUID | None = None
    business_date: date

    subtotal: Money
    discount_tier: Money = 0
    discount_promo: Money = 0
    total: Money
    tendered_amount: Money | None = None
    change_amount: Money | None = None

    promotion_id: str | None = None
    authorized_by_employee_id: str | None = None

    lines: list[SaleLinePayload] = Field(min_length=1)
    payments: list[SalePaymentPayload] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_invariants(self) -> SaleCompletedPayload:
        # INV-1 (docs/05 §6) — cũng là CHECK constraint ở DB. Kiểm ở đây để sự kiện sai
        # không bao giờ rời cửa hàng.
        if self.subtotal != self.total + self.discount_tier + self.discount_promo:
            raise ValueError("INV-1 vi phạm: subtotal != total + discount_tier + discount_promo")
        # INV-2 — tổng thanh toán phải bằng tổng đơn.
        if sum(p.amount for p in self.payments) != self.total:
            raise ValueError("INV-2 vi phạm: SUM(payments.amount) != total")
        return self


class SaleReturnedPayload(_Strict):
    """docs/12 §3.2. KHÔNG sửa `SaleCompleted` gốc — trả hàng là giao dịch mới."""

    sale_id: uuid.UUID
    original_sale_id: uuid.UUID
    voided_by_sale_id: uuid.UUID | None = None
    lines: list[SaleLinePayload] = Field(min_length=1)
    reason: str = "CUSTOMER_RETURN"


class PointsPayload(_Strict):
    """docs/12 §3.3 — ánh xạ ~1-1 từ một dòng `point_ledger`.

    ⚠️ `event_id` của envelope CHÍNH LÀ `point_ledger.event_id` ở cả hai đầu.
    Không sinh ID mới khi chuyển tiếp, nếu không idempotency vỡ.
    """

    customer_id: uuid.UUID
    sale_id: uuid.UUID | None = None
    delta: int
    reason: PointReason
    metadata: dict[str, object] = Field(default_factory=dict)


class CustomerPayload(_Strict):
    """docs/12 §3.4. Chỉ hash + ciphertext — ràng buộc #10: không PII đọc được."""

    customer_id: uuid.UUID
    phone_hash: str
    phone_enc: str | None = None
    name_enc: str | None = None
    created_locally_at_store: str | None = None


class ShiftClosedPayload(_Strict):
    shift_id: uuid.UUID
    business_date: date
    opened_by_employee_id: str
    closed_by_employee_id: str | None = None
    opened_at: datetime
    closed_at: datetime
    opening_cash: Money
    expected_cash: Money
    counted_cash: Money
    variance: Money
    variance_note: str | None = None


PAYLOAD_BY_TYPE: dict[str, type[BaseModel]] = {
    "SaleCompleted": SaleCompletedPayload,
    "SaleReturned": SaleReturnedPayload,
    "PointsEarned": PointsPayload,
    "PointsReturned": PointsPayload,
    "PointsAdjusted": PointsPayload,
    "CustomerCreated": CustomerPayload,
    "CustomerUpdated": CustomerPayload,
    "ShiftClosed": ShiftClosedPayload,
}


# ═══════════════════════════ Envelope ═══════════════════════════


class TraceContext(BaseModel):
    """Ràng buộc #7 — traceparent nằm TRONG payload outbox, không phải HTTP header.

    Độ trễ store→central có thể hàng giờ, nên đầu nhận dùng **span link**, không phải
    parent-child (docs/10-observability.md §4).
    """

    model_config = ConfigDict(extra="allow")

    traceparent: str | None = None
    tracestate: str | None = None


class EventEnvelope(_Lenient):
    """docs/12 §2. Một hình dạng cho mọi loại sự kiện — sync worker không rẽ nhánh theo type."""

    event_id: uuid.UUID
    event_type: EventType
    schema_version: int = SCHEMA_VERSION
    store_id: str
    occurred_at: datetime
    payload: dict[str, object]
    trace: TraceContext = Field(default_factory=TraceContext)

    def parsed_payload(self) -> BaseModel:
        """Ép payload về model đúng loại.

        Gọi ở đầu NHẬN, sau khi đã kiểm `schema_version`. Sai kiểu → ném ValidationError
        → central ghi dead-letter thay vì đoán ý nghĩa (docs/12 §4).
        """
        model = PAYLOAD_BY_TYPE[self.event_type]
        return model.model_validate(self.payload)


class IngestResult(BaseModel):
    """Phản hồi `POST /events` — docs/13 §2.

    Từng sự kiện độc lập: một sự kiện lỗi không được làm rớt cả lô, nếu không worker
    retry cả lô và gây lặp vô ích cho phần đã thành công.
    """

    accepted: list[uuid.UUID] = Field(default_factory=list)
    rejected: list[RejectedEvent] = Field(default_factory=list)


class RejectedEvent(BaseModel):
    event_id: uuid.UUID
    reason: str


IngestResult.model_rebuild()
