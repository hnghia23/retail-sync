"""Use case `PlaceSale` — đường đi quan trọng nhất của toàn hệ thống.

docs/13 §3 (hợp đồng HTTP) · docs/03 §4.1 (sơ đồ tuần tự) · docs/12 §3.1 (sự kiện).

## Bốn quy tắc, theo thứ tự ưu tiên khi chúng xung đột

1. **Không gọi trung tâm.** Không có bước nào ở đây chạm mạng ra ngoài cửa hàng. Tra cứu
   khách xảy ra *trước* khi vào use case này và tự nó không bao giờ lỗi ra ngoài
   (docs/13 §4). Vì vậy không có mã lỗi nào cho "trung tâm không tới được".

2. **Một transaction.** `sale` + `sale_line` + `sale_payment` + `point_ledger_local` +
   `outbox` cùng commit hoặc cùng rollback. Transaction do tầng route mở và mọi adapter
   dùng chung nó — use case không tự quản lý transaction, chỉ giả định mình đang ở trong một.

3. **Loyalty lỗi không được chặn bán hàng.** ADR-004 ghi rõ: một bug nặng ở loyalty có thể
   kéo sập cả POS nếu không cô lập. Nên phần tích điểm được bọc riêng và suy giảm có kiểm
   soát — đơn vẫn hoàn tất, chỉ không có điểm.

4. **Xác thực trước, ghi sau.** Mọi lỗi `4xx` phải xảy ra khi chưa insert gì (docs/13 §3).

## Điều KHÔNG nằm ở đây

Phân quyền (vai trò `cashier`), xác thực JWT, và việc mở transaction — đều thuộc tầng
adapter/route. Use case nhận đầu vào đã hợp lệ về mặt định danh.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from edge.pos.application.ports import (
    SaleLineToPersist,
    SalePaymentToPersist,
    SaleToPersist,
)
from edge.pos.domain.pricing import CartLine, PricingError, price_cart
from shared.events import SaleCompletedPayload, SaleLinePayload, SalePaymentPayload
from shared.types import Money, PaymentMethod, new_event_id, utcnow

if TYPE_CHECKING:
    from edge.pos.application.ports import (
        CustomerSnapshot,
        EventPublisherPort,
        LoyaltyPort,
        OpenShift,
        ProductCatalogPort,
        SaleRepositoryPort,
        ShiftPort,
    )
    from shared.config import PricingRules

log = logging.getLogger(__name__)


class SaleRejectedError(Exception):
    """Đơn bị từ chối trước khi ghi gì. `code` ánh xạ sang HTTP ở tầng route (docs/13 §3).

    | code                | HTTP |
    |---------------------|------|
    | `EMPTY_CART`        | 400  |
    | `PAYMENT_MISMATCH`  | 400  |
    | `INVALID_PRICING`   | 400  |
    | `PRODUCT_NOT_FOUND` | 404  |
    | `SHIFT_CLOSED`      | 409  |
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RequestedLine:
    product_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class RequestedPayment:
    method: PaymentMethod
    amount: Money
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class PlaceSaleCommand:
    """Đầu vào — khớp body `POST /sales` (docs/13 §3).

    `customer` đã được phân giải xong (có thể là `CustomerSnapshot.anonymous()`); use case
    không tự đi tra vì việc đó có thể chạm mạng.
    """

    employee_id: str
    shift_id: uuid.UUID
    lines: list[RequestedLine]
    payments: list[RequestedPayment]
    customer: CustomerSnapshot
    tendered_amount: Money | None = None
    promotion_id: str | None = None
    promo_discount_pct: int = 0
    occurred_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class SaleReceipt:
    """Đầu ra — khớp response `201` ở docs/13 §3."""

    sale_id: uuid.UUID
    status: str
    subtotal: Money
    discount_tier: Money
    discount_promo: Money
    total: Money
    change_amount: Money | None
    tier_discount_pct: int
    #: `None` = loyalty suy giảm, đơn vẫn hoàn tất. Khác hẳn `0` = khách vãng lai.
    points_earned: int | None


async def place_sale(
    command: PlaceSaleCommand,
    *,
    catalog: ProductCatalogPort,
    shifts: ShiftPort,
    sales: SaleRepositoryPort,
    events: EventPublisherPort,
    loyalty: LoyaltyPort,
    rules: PricingRules,
) -> SaleReceipt:
    """Chốt một đơn hàng. Gọi bên trong một transaction đang mở."""
    shift = await _require_open_shift(shifts, command.shift_id)
    cart = await _build_cart(catalog, command.lines)

    tier_pct = _tier_discount_pct(loyalty, command.customer)
    try:
        priced = price_cart(
            cart,
            rules=rules,
            tier_discount_pct=tier_pct,
            promo_discount_pct=command.promo_discount_pct,
        )
    except PricingError as exc:
        raise SaleRejectedError("INVALID_PRICING", str(exc)) from exc

    _require_payments_match_total(command.payments, priced.total)
    change_amount = _change_amount(command)

    sale_id = new_event_id()  # UUIDv7 sinh tại cửa hàng (docs/05 §1)
    lines = tuple(
        SaleLineToPersist(
            line_no=line.line_no,
            product_id=line.product_id,
            quantity=line.quantity,
            unit_price=line.unit_price,
            line_total=line.line_total,
        )
        for line in priced.lines
    )
    payments = tuple(
        SalePaymentToPersist(seq=i, method=p.method, amount=p.amount, reference=p.reference)
        for i, p in enumerate(command.payments, start=1)
    )

    # ─── Từ đây trở đi là ghi. Mọi lỗi 4xx đã xảy ra ở trên. ───
    await sales.save(
        SaleToPersist(
            sale_id=sale_id,
            store_id=shift.store_id,
            shift_id=shift.shift_id,
            business_date=shift.business_date,
            employee_id=command.employee_id,
            customer_id=command.customer.customer_id,
            occurred_at=command.occurred_at,
            subtotal=priced.subtotal,
            discount_tier=priced.discount_tier,
            discount_promo=priced.discount_promo,
            total=priced.total,
            tendered_amount=command.tendered_amount,
            change_amount=change_amount,
            promotion_id=command.promotion_id,
            status="COMPLETED",
            original_sale_id=None,
            lines=lines,
            payments=payments,
        )
    )

    points_earned = await _accrue_points(
        loyalty, command=command, shift=shift, sale_id=sale_id, total=priced.total
    )

    await events.publish(
        event_type="SaleCompleted",
        occurred_at=command.occurred_at,
        payload=SaleCompletedPayload(
            sale_id=sale_id,
            shift_id=shift.shift_id,
            employee_id=command.employee_id,
            customer_id=command.customer.customer_id,
            business_date=shift.business_date,
            subtotal=priced.subtotal,
            discount_tier=priced.discount_tier,
            discount_promo=priced.discount_promo,
            total=priced.total,
            tendered_amount=command.tendered_amount,
            change_amount=change_amount,
            promotion_id=command.promotion_id,
            lines=[
                SaleLinePayload(
                    line_no=line.line_no,
                    product_id=line.product_id,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    line_total=line.line_total,
                )
                for line in lines
            ],
            payments=[
                SalePaymentPayload(
                    seq=p.seq, method=p.method, amount=p.amount, reference=p.reference
                )
                for p in payments
            ],
        ),
    )

    return SaleReceipt(
        sale_id=sale_id,
        status="COMPLETED",
        subtotal=priced.subtotal,
        discount_tier=priced.discount_tier,
        discount_promo=priced.discount_promo,
        total=priced.total,
        change_amount=change_amount,
        tier_discount_pct=tier_pct,
        points_earned=points_earned,
    )


# ═══════════════════════ Các bước ═══════════════════════


async def _require_open_shift(shifts: ShiftPort, shift_id: uuid.UUID) -> OpenShift:
    shift = await shifts.get_open_shift(shift_id)
    if shift is None:
        raise SaleRejectedError("SHIFT_CLOSED", f"Ca {shift_id} không tồn tại hoặc đã đóng")
    return shift


async def _build_cart(catalog: ProductCatalogPort, lines: list[RequestedLine]) -> list[CartLine]:
    """Gộp giá cho mọi dòng bằng MỘT lần truy vấn.

    Không tra từng dòng một: giỏ 20 món sẽ thành 20 round-trip, và `GET /products` có ngân
    sách p95 < 100ms (docs/13 §1).
    """
    if not lines:
        raise SaleRejectedError("EMPTY_CART", "Giỏ hàng rỗng")

    prices = await catalog.prices_for([line.product_id for line in lines])
    missing = [line.product_id for line in lines if line.product_id not in prices]
    if missing:
        raise SaleRejectedError(
            "PRODUCT_NOT_FOUND",
            f"Không có trong product_cache hoặc không bán được: {', '.join(sorted(set(missing)))}",
        )

    try:
        return [CartLine(line.product_id, prices[line.product_id], line.quantity) for line in lines]
    except PricingError as exc:
        raise SaleRejectedError("INVALID_PRICING", str(exc)) from exc


def _tier_discount_pct(loyalty: LoyaltyPort, customer: CustomerSnapshot) -> int:
    """Suy giảm có kiểm soát #1 (ADR-004).

    Hạng chưa xác định (trung tâm không tới được lúc tra khách) → 0%. Bán hụt chiết khấu
    còn bù được bằng phiếu; chặn đứng quầy thì vi phạm nguyên tắc kiến trúc #1.
    """
    if customer.customer_id is None or customer.tier_unknown:
        return 0
    try:
        return loyalty.tier_discount_pct(customer.tier)
    except Exception:
        log.warning("loyalty.tier_discount_pct lỗi, bán tiếp với 0%%", exc_info=True)
        return 0


def _require_payments_match_total(payments: list[RequestedPayment], total: Money) -> None:
    """INV-2 ở tầng use case.

    DB cũng có constraint trigger cho bất biến này, nhưng trigger nổ lúc COMMIT — quá muộn
    để trả lỗi `400` gọn gàng cho thu ngân. Kiểm ở đây để lỗi thường gặp nhất (bấm nhầm số
    tiền) có thông báo rõ ràng thay vì một exception từ Postgres.
    """
    if not payments:
        raise SaleRejectedError("PAYMENT_MISMATCH", "Đơn không có hình thức thanh toán nào")
    paid = sum(p.amount for p in payments)
    if paid != total:
        raise SaleRejectedError("PAYMENT_MISMATCH", f"Tổng thanh toán {paid} khác tổng đơn {total}")


def _change_amount(command: PlaceSaleCommand) -> Money | None:
    """G2 — tiền khách đưa và tiền thối.

    Chỉ có nghĩa khi thu ngân nhập `tendered_amount` (thanh toán tiền mặt). Đưa thiếu tiền
    là sai nghiệp vụ, không phải tiền thối âm.
    """
    if command.tendered_amount is None:
        return None
    cash = sum(p.amount for p in command.payments if p.method == "CASH")
    if cash == 0:
        # Thanh toán 100% thẻ/ví mà vẫn có `tendered_amount`: không có tiền mặt để thối.
        # Trả `None` thay vì một số âm vô nghĩa.
        return None
    if command.tendered_amount < cash:
        raise SaleRejectedError(
            "PAYMENT_MISMATCH",
            f"Khách đưa {command.tendered_amount} nhưng phần tiền mặt là {cash}",
        )
    return command.tendered_amount - cash


async def _accrue_points(
    loyalty: LoyaltyPort,
    *,
    command: PlaceSaleCommand,
    shift: OpenShift,
    sale_id: uuid.UUID,
    total: Money,
) -> int | None:
    """Suy giảm có kiểm soát #2 — chỗ quan trọng nhất của cả file.

    `sale.save()` đã chạy. Nếu loyalty ném lỗi và ta để nó lan ra, transaction rollback và
    **đơn hàng biến mất** vì một lỗi ở module phụ. Đó đúng là kịch bản ADR-004 cảnh báo.

    Đánh đổi: đơn được ghi mà không có dòng ledger. Chấp nhận được vì `SaleCompleted` vẫn
    lên trung tâm với đủ `customer_id` và `total`, nên job đối soát dựng lại được điểm
    thiếu. Ngược lại, mất hẳn một đơn hàng thì không dựng lại được từ đâu.
    """
    if command.customer.customer_id is None:
        return 0  # khách vãng lai — 0 điểm, không phải lỗi

    try:
        accrual = await loyalty.accrue_for_sale(
            customer_id=command.customer.customer_id,
            sale_id=sale_id,
            store_id=shift.store_id,
            amount=total,
            occurred_at=command.occurred_at,
        )
    except Exception:
        log.error(
            "Tích điểm lỗi cho sale_id=%s; đơn VẪN hoàn tất, điểm sẽ được đối soát bù",
            sale_id,
            exc_info=True,
        )
        return None
    return accrual.delta
