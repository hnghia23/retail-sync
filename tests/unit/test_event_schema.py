"""Hợp đồng sự kiện — docs/12-event-schema.md.

Test này canh **ranh giới** giữa cửa hàng và trung tâm. Nó fail nghĩa là một trong hai
đầu sắp hiểu sai đầu kia — đúng lỗi đã giết v1.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from shared.events import EventEnvelope, SaleCompletedPayload


def _valid_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sale_id": uuid.uuid4(),
        "shift_id": uuid.uuid4(),
        "employee_id": "emp-007",
        "business_date": date(2026, 9, 11),
        "subtotal": 560_000,
        "discount_tier": 28_000,
        "discount_promo": 0,
        "total": 532_000,
        "lines": [
            {
                "line_no": 1,
                "product_id": "SKU-001",
                "quantity": 2,
                "unit_price": 280_000,
                "line_total": 560_000,
            }
        ],
        "payments": [{"seq": 1, "method": "CASH", "amount": 532_000}],
    }
    return base | overrides


def test_sale_completed_accepts_valid_payload() -> None:
    payload = SaleCompletedPayload.model_validate(_valid_payload())
    assert payload.total == 532_000


def test_inv1_violation_is_rejected_before_leaving_store() -> None:
    """INV-1: subtotal = total + discount_tier + discount_promo.

    Đây cũng là CHECK constraint ở DB; kiểm ở tầng sự kiện để dữ liệu sai không bao giờ
    rời cửa hàng.
    """
    with pytest.raises(ValidationError, match="INV-1"):
        SaleCompletedPayload.model_validate(_valid_payload(total=500_000))


def test_inv2_violation_is_rejected() -> None:
    """INV-2: SUM(sale_payment.amount) = sale.total — G1, một đơn nhiều hình thức trả."""
    with pytest.raises(ValidationError, match="INV-2"):
        SaleCompletedPayload.model_validate(
            _valid_payload(payments=[{"seq": 1, "method": "CASH", "amount": 1}])
        )


def test_split_payment_is_accepted() -> None:
    """Cổng tuần 2: bán một đơn thanh toán tách (tiền mặt + thẻ)."""
    payload = SaleCompletedPayload.model_validate(
        _valid_payload(
            payments=[
                {"seq": 1, "method": "CASH", "amount": 300_000},
                {"seq": 2, "method": "CARD", "amount": 232_000, "reference": "VISA-...1234"},
            ]
        )
    )
    assert sum(p.amount for p in payload.payments) == payload.total


def test_envelope_tolerates_unknown_field() -> None:
    """docs/12 §4: thêm field optional KHÔNG tăng schema_version.

    Consumer cũ phải bỏ qua field lạ thay vì nổ — nếu không, mọi lần thêm field sẽ là một
    lần triển khai đồng thời hai đầu, điều không làm được với 2000 cửa hàng.
    """
    envelope = EventEnvelope.model_validate(
        {
            "event_id": uuid.uuid4(),
            "event_type": "SaleCompleted",
            "schema_version": 1,
            "store_id": "store-042",
            "occurred_at": datetime(2026, 9, 11, 3, 12, 44, tzinfo=UTC),
            "payload": _valid_payload(),
            "trace": {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
            "field_moi_toanh": "consumer cu phai bo qua",
        }
    )
    assert envelope.parsed_payload().total == 532_000  # type: ignore[attr-defined]


def test_sender_payload_rejects_typo() -> None:
    """Phía GỬI thì ngược lại: cấm field lạ, để lỗi chính tả không lọt ra khỏi cửa hàng."""
    with pytest.raises(ValidationError):
        SaleCompletedPayload.model_validate(_valid_payload(totl=532_000))
