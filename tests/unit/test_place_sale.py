"""Use case `PlaceSale` — docs/13 §3.

Chạy bằng **fake**, không cần Docker: đó là toàn bộ lý do các port không mang kiểu hạ tầng
nào. Test tích hợp với Postgres thật (một transaction, outbox, ledger) là
`tests/integration/test_place_sale.py` — việc khác, tầng khác.

Bốn nhóm khẳng định, theo thứ tự quan trọng:
  1. Đơn đúng thì ghi đúng ba bảng + đúng một sự kiện outbox.
  2. Mọi lỗi 4xx xảy ra khi CHƯA ghi gì.
  3. Loyalty hỏng KHÔNG làm mất đơn hàng.
  4. Sự kiện outbox khớp hợp đồng docs/12 §3.1.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest

from edge.pos.application.place_sale import (
    PlaceSaleCommand,
    RequestedLine,
    RequestedPayment,
    SaleRejectedError,
    place_sale,
)
from edge.pos.application.ports import (
    CustomerSnapshot,
    OpenShift,
    PointsAccrual,
    SaleToPersist,
)
from shared.config import PricingRules
from shared.types import Money, Tier

SHIFT_ID = uuid.UUID("01935e1c-0000-7000-8000-000000000001")
CUSTOMER_ID = uuid.UUID("01935abc-0000-7000-8000-000000000001")
OCCURRED_AT = datetime(2026, 9, 17, 3, 12, 44, tzinfo=UTC)

CATALOG_PRICES: dict[str, Money] = {"SKU-001": 150_000, "SKU-002": 100_000, "SKU-003": 50_000}


# ═══════════════════════ Fake ═══════════════════════


@dataclass
class FakeCatalog:
    prices: dict[str, Money] = field(default_factory=lambda: dict(CATALOG_PRICES))
    calls: int = 0

    async def prices_for(self, product_ids: Any) -> dict[str, Money]:
        self.calls += 1
        return {pid: self.prices[pid] for pid in product_ids if pid in self.prices}


@dataclass
class FakeShifts:
    shift: OpenShift | None = field(
        default_factory=lambda: OpenShift(SHIFT_ID, "store-001", date(2026, 9, 17))
    )

    async def get_open_shift(self, shift_id: uuid.UUID) -> OpenShift | None:
        return self.shift if self.shift and self.shift.shift_id == shift_id else None


@dataclass
class FakeSales:
    saved: list[SaleToPersist] = field(default_factory=list)

    async def save(self, sale: SaleToPersist) -> None:
        self.saved.append(sale)


@dataclass
class FakeEvents:
    published: list[dict[str, Any]] = field(default_factory=list)

    async def publish(self, **kwargs: Any) -> uuid.UUID:
        self.published.append(kwargs)
        return kwargs.get("event_id") or uuid.uuid4()


@dataclass
class FakeLoyalty:
    discount_pct: int = 5
    accrued: list[dict[str, Any]] = field(default_factory=list)
    raise_on_discount: bool = False
    raise_on_accrue: bool = False

    async def resolve_customer(self, *, phone_hash: str) -> CustomerSnapshot:
        return CustomerSnapshot.anonymous()

    def tier_discount_pct(self, tier: Tier) -> int:
        if self.raise_on_discount:
            raise RuntimeError("loyalty hong")
        return self.discount_pct

    async def accrue_for_sale(self, **kwargs: Any) -> PointsAccrual:
        if self.raise_on_accrue:
            raise RuntimeError("loyalty hong")
        self.accrued.append(kwargs)
        return PointsAccrual(event_id=uuid.uuid4(), delta=int(kwargs["amount"]) // 10_000)

    async def reverse_for_return(self, **kwargs: Any) -> PointsAccrual:  # pragma: no cover
        raise NotImplementedError


@dataclass
class Fakes:
    catalog: FakeCatalog = field(default_factory=FakeCatalog)
    shifts: FakeShifts = field(default_factory=FakeShifts)
    sales: FakeSales = field(default_factory=FakeSales)
    events: FakeEvents = field(default_factory=FakeEvents)
    loyalty: FakeLoyalty = field(default_factory=FakeLoyalty)

    async def run(self, command: PlaceSaleCommand, rules: PricingRules | None = None) -> Any:
        return await place_sale(
            command,
            catalog=self.catalog,
            shifts=self.shifts,
            sales=self.sales,
            events=self.events,
            loyalty=self.loyalty,
            rules=rules or PricingRules(_env_file=None),
        )


def known_customer(tier: Tier = "GOLD") -> CustomerSnapshot:
    return CustomerSnapshot(customer_id=CUSTOMER_ID, tier=tier, balance=100, lifetime_earned=5_000)


def a_command(**overrides: Any) -> PlaceSaleCommand:
    defaults: dict[str, Any] = {
        "employee_id": "emp-007",
        "shift_id": SHIFT_ID,
        "lines": [RequestedLine("SKU-001", 2), RequestedLine("SKU-002", 1)],
        "payments": [RequestedPayment("CASH", 380_000)],
        "customer": known_customer(),
        "occurred_at": OCCURRED_AT,
    }
    return PlaceSaleCommand(**(defaults | overrides))


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


# ═══════════════════════ 1. Đường chính ═══════════════════════


async def test_sale_is_priced_persisted_and_published(fakes: Fakes) -> None:
    """400.000 tạm tính, khách GOLD giảm 5% → 380.000."""
    receipt = await fakes.run(a_command())

    assert receipt.status == "COMPLETED"
    assert (receipt.subtotal, receipt.discount_tier, receipt.total) == (400_000, 20_000, 380_000)
    assert receipt.tier_discount_pct == 5
    assert receipt.points_earned == 38  # 380.000đ ÷ 10.000

    saved = fakes.sales.saved[0]
    assert saved.store_id == "store-001"  # lấy TỪ CA, không từ request
    assert saved.business_date == date(2026, 9, 17)
    assert len(saved.lines) == 2
    assert len(saved.payments) == 1

    assert len(fakes.events.published) == 1
    assert fakes.events.published[0]["event_type"] == "SaleCompleted"


async def test_split_payment_across_methods(fakes: Fakes) -> None:
    """G1 + cổng tuần 2: một đơn, nhiều hình thức thanh toán."""
    receipt = await fakes.run(
        a_command(
            payments=[
                RequestedPayment("CASH", 200_000),
                RequestedPayment("CARD", 180_000, reference="VISA-1234"),
            ]
        )
    )
    payments = fakes.sales.saved[0].payments
    assert [p.seq for p in payments] == [1, 2]
    assert sum(p.amount for p in payments) == receipt.total


async def test_anonymous_customer_gets_no_discount_and_zero_points(fakes: Fakes) -> None:
    """Khách vãng lai: 0 điểm là kết quả ĐÚNG, không phải lỗi."""
    receipt = await fakes.run(
        a_command(
            customer=CustomerSnapshot.anonymous(),
            payments=[RequestedPayment("CASH", 400_000)],
        )
    )
    assert receipt.tier_discount_pct == 0
    assert receipt.total == 400_000
    assert receipt.points_earned == 0
    assert fakes.loyalty.accrued == []


async def test_catalog_is_queried_once_for_whole_cart(fakes: Fakes) -> None:
    """Giỏ 20 món không được thành 20 round-trip (`GET /products` có ngân sách p95 < 100ms)."""
    await fakes.run(
        a_command(
            lines=[RequestedLine("SKU-003", 1) for _ in range(20)],
            payments=[RequestedPayment("CASH", 950_000)],
        )
    )
    assert fakes.catalog.calls == 1


async def test_change_amount_is_computed_for_cash(fakes: Fakes) -> None:
    """G2: khách đưa 400.000 cho đơn 380.000 → thối 20.000."""
    receipt = await fakes.run(a_command(tendered_amount=400_000))
    assert receipt.change_amount == 20_000
    assert fakes.sales.saved[0].tendered_amount == 400_000


async def test_no_change_when_paying_entirely_by_card(fakes: Fakes) -> None:
    """Không có tiền mặt thì không có tiền thối — `None`, không phải một số âm."""
    receipt = await fakes.run(
        a_command(payments=[RequestedPayment("CARD", 380_000)], tendered_amount=380_000)
    )
    assert receipt.change_amount is None


async def test_partial_cash_change_uses_cash_portion_only(fakes: Fakes) -> None:
    """Trả 180.000 bằng thẻ + đưa 250.000 tiền mặt cho phần 200.000 → thối 50.000."""
    receipt = await fakes.run(
        a_command(
            payments=[RequestedPayment("CARD", 180_000), RequestedPayment("CASH", 200_000)],
            tendered_amount=250_000,
        )
    )
    assert receipt.change_amount == 50_000


async def test_tier_is_read_at_order_time_not_recomputed(fakes: Fakes) -> None:
    """FR-L05: hạng đọc lúc bắt đầu đơn, không đổi giữa chừng.

    Use case nhận `CustomerSnapshot` đã phân giải sẵn — nó KHÔNG tự đi tra khách, vì việc
    đó có thể chạm mạng và sẽ vi phạm quy tắc "không gọi trung tâm trong luồng chốt đơn".
    """
    await fakes.run(a_command(customer=known_customer("PLATINUM")))
    assert fakes.sales.saved[0].customer_id == CUSTOMER_ID


# ═══════════════════════ 2. Lỗi 4xx: chưa ghi gì ═══════════════════════


async def test_closed_shift_is_rejected(fakes: Fakes) -> None:
    """docs/13 §3: `409`, có rollback (ở đây: chưa insert gì)."""
    fakes.shifts.shift = None
    with pytest.raises(SaleRejectedError) as exc:
        await fakes.run(a_command())
    assert exc.value.code == "SHIFT_CLOSED"
    assert fakes.sales.saved == []
    assert fakes.events.published == []


async def test_unknown_product_is_rejected(fakes: Fakes) -> None:
    """`404` — và tên sản phẩm thiếu phải có trong thông báo để thu ngân biết quét lại món nào."""
    with pytest.raises(SaleRejectedError) as exc:
        await fakes.run(a_command(lines=[RequestedLine("SKU-999", 1)]))
    assert exc.value.code == "PRODUCT_NOT_FOUND"
    assert "SKU-999" in str(exc.value)
    assert fakes.sales.saved == []


async def test_unsellable_product_is_rejected_like_missing(fakes: Fakes) -> None:
    """G7: hàng ngừng bán không nằm trong kết quả `prices_for`, xử lý y như không tồn tại."""
    del fakes.catalog.prices["SKU-002"]
    with pytest.raises(SaleRejectedError) as exc:
        await fakes.run(a_command())
    assert exc.value.code == "PRODUCT_NOT_FOUND"


async def test_empty_cart_is_rejected(fakes: Fakes) -> None:
    with pytest.raises(SaleRejectedError) as exc:
        await fakes.run(a_command(lines=[]))
    assert exc.value.code == "EMPTY_CART"


async def test_payment_sum_mismatch_is_rejected(fakes: Fakes) -> None:
    """INV-2 chặn ở use case để thu ngân nhận `400` rõ ràng.

    DB cũng có constraint trigger cho bất biến này, nhưng trigger nổ lúc COMMIT — quá muộn
    để dịch thành thông báo tử tế.
    """
    with pytest.raises(SaleRejectedError) as exc:
        await fakes.run(a_command(payments=[RequestedPayment("CASH", 999)]))
    assert exc.value.code == "PAYMENT_MISMATCH"
    assert fakes.sales.saved == []


async def test_no_payment_is_rejected(fakes: Fakes) -> None:
    with pytest.raises(SaleRejectedError) as exc:
        await fakes.run(a_command(payments=[]))
    assert exc.value.code == "PAYMENT_MISMATCH"


async def test_tendered_less_than_cash_is_rejected(fakes: Fakes) -> None:
    """Khách đưa thiếu tiền là sai nghiệp vụ, không phải tiền thối âm."""
    with pytest.raises(SaleRejectedError) as exc:
        await fakes.run(a_command(tendered_amount=100_000))
    assert exc.value.code == "PAYMENT_MISMATCH"


# ═══════════════════════ 3. Loyalty hỏng: đơn VẪN hoàn tất (ADR-004) ═══════════════════════


async def test_loyalty_discount_failure_degrades_to_zero_percent(fakes: Fakes) -> None:
    """Suy giảm #1: không tính được chiết khấu hạng thì bán giá gốc, không chặn quầy."""
    fakes.loyalty.raise_on_discount = True
    receipt = await fakes.run(a_command(payments=[RequestedPayment("CASH", 400_000)]))
    assert receipt.tier_discount_pct == 0
    assert receipt.total == 400_000
    assert receipt.status == "COMPLETED"


async def test_loyalty_accrual_failure_does_not_lose_the_sale(fakes: Fakes) -> None:
    """⚠️ Khẳng định quan trọng nhất của cả file.

    Nếu lỗi tích điểm lan ra ngoài, transaction rollback và **đơn hàng biến mất** vì một
    lỗi ở module phụ — đúng kịch bản ADR-004 cảnh báo. Đơn phải được ghi, sự kiện phải lên
    trung tâm; điểm thiếu thì job đối soát dựng lại được từ `SaleCompleted`.
    """
    fakes.loyalty.raise_on_accrue = True
    receipt = await fakes.run(a_command())

    assert receipt.status == "COMPLETED"
    assert len(fakes.sales.saved) == 1
    assert len(fakes.events.published) == 1
    # `None` = suy giảm, phân biệt được với `0` = khách vãng lai.
    assert receipt.points_earned is None


# ═══════════════════════ 4. Sự kiện khớp hợp đồng docs/12 §3.1 ═══════════════════════


async def test_event_payload_matches_persisted_sale(fakes: Fakes) -> None:
    """Sự kiện và bản ghi DB phải là CÙNG một sự thật — lệch nhau là lỗi âm thầm tệ nhất."""
    receipt = await fakes.run(a_command())
    payload = fakes.events.published[0]["payload"]
    saved = fakes.sales.saved[0]

    assert payload.sale_id == saved.sale_id == receipt.sale_id
    assert payload.total == saved.total
    assert payload.business_date == saved.business_date
    assert len(payload.lines) == len(saved.lines)
    assert len(payload.payments) == len(saved.payments)


async def test_event_carries_lines_and_payments_together(fakes: Fakes) -> None:
    """docs/12 §3.1: MỘT sự kiện mang cả dòng hàng lẫn thanh toán, không tách ba sự kiện con.

    Tách ra thì trung tâm phải ghép lại và có race giữa các sự kiện con.
    """
    await fakes.run(
        a_command(payments=[RequestedPayment("CASH", 200_000), RequestedPayment("CARD", 180_000)])
    )
    assert len(fakes.events.published) == 1
    payload = fakes.events.published[0]["payload"]
    assert len(payload.lines) == 2
    assert len(payload.payments) == 2


async def test_event_payload_enforces_inv1_and_inv2(fakes: Fakes) -> None:
    """`SaleCompletedPayload` tự kiểm INV-1/INV-2 khi khởi tạo (shared/events.py).

    Nghĩa là: nếu use case tính sai, sự kiện không được tạo ra — sai lệch không bao giờ
    rời khỏi cửa hàng. Test này khóa chặt việc đó vẫn còn hiệu lực.
    """
    await fakes.run(a_command())
    payload = fakes.events.published[0]["payload"]
    assert payload.subtotal == payload.total + payload.discount_tier + payload.discount_promo
    assert sum(p.amount for p in payload.payments) == payload.total


async def test_sale_id_is_uuidv7_generated_at_store(fakes: Fakes) -> None:
    """docs/05 §1: UUIDv7 sinh TẠI CỬA HÀNG, sắp theo thời gian, duy nhất toàn cục."""
    first = await fakes.run(a_command())
    second = await fakes.run(a_command())
    assert first.sale_id.version == 7
    assert str(first.sale_id) < str(second.sale_id)
