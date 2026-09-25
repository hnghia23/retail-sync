"""Interface mà `pos` CẦN từ bên ngoài — ADR-004 "việc bắt buộc phải làm".

Viết trước code nghiệp vụ, đúng thứ tự ở docs/11 §5: ranh giới trước, chi tiết sau.

Hai quy tắc định hình file này:

1. **Không có kiểu hạ tầng nào xuất hiện ở đây.** Không `AsyncSession`, không `Connection`.
   Adapter nhận session lúc khởi tạo và giữ nó bên trong; use case chỉ thấy các phương
   thức nghiệp vụ. Nhờ vậy `application` test được bằng fake, không cần Docker.

2. **`Protocol` chứ không `ABC`.** `pos` không import implementation, implementation không
   import `pos`. Cạnh phụ thuộc duy nhất giữa hai module là `pos` → `edge.loyalty.api` —
   cửa công khai, không phải tầng trong.

Mọi adapter được dựng trên CÙNG một transaction do tầng route mở — đó là điều làm cho
"lưu đơn + tích điểm + ghi outbox" thành một hành động nguyên tử (docs/03 §4.1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

# `CustomerSnapshot` và `PointsAccrual` do `loyalty` SỞ HỮU, vì nó là bên cung cấp: để
# `pos` định nghĩa chúng thì `loyalty` phải import `pos` để dựng giá trị trả về, tạo cạnh
# phụ thuộc ngược chiều mà ADR-004 cấm. Nhập lại ở đây cho `pos` dùng như kiểu của mình.
from edge.loyalty.api import CustomerSnapshot, PointsAccrual
from shared.types import Money, PaymentMethod, Tier

__all__ = [
    "CustomerSnapshot",
    "EventPublisherPort",
    "LoyaltyPort",
    "OpenShift",
    "PointsAccrual",
    "ProductCatalogPort",
    "SaleLineToPersist",
    "SalePaymentToPersist",
    "SaleRepositoryPort",
    "SaleToPersist",
    "ShiftLifecyclePort",
    "ShiftPort",
    "ShiftToClose",
]

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

    from shared.events import EventType


# ═══════════════════════ Kiểu dữ liệu qua ranh giới ═══════════════════════


@dataclass(frozen=True, slots=True)
class OpenShift:
    """Ca đang mở. `business_date` lấy TỪ CA, không suy từ `now()` (G5, B02 Z03)."""

    shift_id: uuid.UUID
    store_id: str
    business_date: date


@dataclass(frozen=True, slots=True)
class ShiftToClose:
    """Ca đang mở, đã KHÓA để đóng — mọi thứ `ShiftClosed` cần lấy từ chính ca."""

    shift_id: uuid.UUID
    store_id: str
    business_date: date
    opened_by_employee_id: str
    opened_at: datetime
    opening_cash: Money


@dataclass(frozen=True, slots=True)
class SaleLineToPersist:
    line_no: int
    product_id: str
    quantity: int
    unit_price: Money
    line_total: Money
    original_sale_id: uuid.UUID | None = None
    original_sale_line_no: int | None = None


@dataclass(frozen=True, slots=True)
class SalePaymentToPersist:
    seq: int
    method: PaymentMethod
    amount: Money
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class SaleToPersist:
    """Ảnh chụp đầy đủ một `sale` — ánh xạ 1-1 sang ba bảng `sale`/`sale_line`/`sale_payment`.

    Ba bảng ghi cùng lúc trong một lần gọi, không phải ba lần gọi riêng: constraint trigger
    INV-2/INV-3 hoãn tới commit, nên chia nhỏ chỉ làm khó đọc mà không lợi gì.
    """

    sale_id: uuid.UUID
    store_id: str
    shift_id: uuid.UUID
    business_date: date
    employee_id: str
    customer_id: uuid.UUID | None
    occurred_at: datetime
    subtotal: Money
    discount_tier: Money
    discount_promo: Money
    total: Money
    tendered_amount: Money | None
    change_amount: Money | None
    promotion_id: str | None
    status: str
    original_sale_id: uuid.UUID | None
    lines: tuple[SaleLineToPersist, ...]
    payments: tuple[SalePaymentToPersist, ...]


# ═══════════════════════ Port ═══════════════════════


class ProductCatalogPort(Protocol):
    """Đọc `product_cache`. Chỉ đọc — master data thuộc sở hữu của trung tâm (docs/05 §2)."""

    async def prices_for(self, product_ids: Sequence[str]) -> dict[str, Money]:
        """Giá của các sản phẩm **bán được** (`is_sellable = true`).

        Sản phẩm không tồn tại hoặc không bán được thì vắng mặt trong dict trả về —
        use case phân biệt được và trả `404` (docs/13 §3).
        """
        ...


class ShiftPort(Protocol):
    async def get_open_shift(self, shift_id: uuid.UUID) -> OpenShift | None:
        """`None` nếu ca không tồn tại hoặc đã đóng → `409` (docs/13 §3)."""
        ...


class ShiftLifecyclePort(Protocol):
    """Mở/đóng ca — FR-P11. Hiện thực ở `PostgresShifts`."""

    async def open(
        self,
        *,
        shift_id: uuid.UUID,
        store_id: str,
        business_date: date,
        employee_id: str,
        opening_cash: Money,
        opened_at: datetime,
    ) -> bool:
        """`False` nếu cửa hàng đã có ca đang mở (mỗi cửa hàng tối đa MỘT ca mở)."""
        ...

    async def find_open_shift(self, store_id: str) -> OpenShift | None: ...

    async def lock_open_for_close(self, shift_id: uuid.UUID, store_id: str) -> ShiftToClose | None:
        """Khóa ca để đóng. PHẢI chặn được đơn đang chốt dở vào ca đó — xem adapter."""
        ...

    async def cash_collected(self, shift_id: uuid.UUID) -> Money:
        """Σ tiền mặt đã thu trong ca (phần tiền mặt của đơn, không phải tiền khách đưa)."""
        ...

    async def mark_closed(
        self,
        *,
        shift_id: uuid.UUID,
        closed_by_employee_id: str,
        closed_at: datetime,
        expected_cash: Money,
        counted_cash: Money,
        variance: Money,
        variance_note: str | None,
    ) -> None: ...


class SaleRepositoryPort(Protocol):
    async def save(self, sale: SaleToPersist) -> None: ...


class EventPublisherPort(Protocol):
    """Ghi outbox. Bắt buộc chạy trong cùng transaction với dữ liệu nghiệp vụ (ADR-003)."""

    async def publish(
        self,
        *,
        event_type: EventType,
        payload: BaseModel,
        occurred_at: datetime,
        event_id: uuid.UUID | None = None,
    ) -> uuid.UUID: ...


class LoyaltyPort(Protocol):
    """Hợp đồng `pos` → `loyalty`. Hiện thực ở `edge/loyalty/api.py`.

    ⚠️ Mọi phương thức ở đây được gọi trong luồng bán hàng, nên use case PHẢI bọc chúng
    bằng suy giảm có kiểm soát: một bug ở loyalty không được làm dừng việc bán hàng
    (ADR-004, "hệ quả tiêu cực").
    """

    async def resolve_customer(self, *, phone_hash: str) -> CustomerSnapshot:
        """Thác đổ Redis → Postgres cục bộ → trung tâm (timeout 500ms) → ẩn danh.

        Nhận `phone_hash`, KHÔNG nhận SĐT thô: số điện thoại là PII và không được đi qua
        ranh giới module (ràng buộc #10). Băm ở tầng route.

        KHÔNG BAO GIỜ ném lỗi ra ngoài: luôn trả về một snapshot.
        """
        ...

    def tier_discount_pct(self, tier: Tier) -> int:
        """Thuần, đồng bộ — `pos` cần nó để tính tiền TRƯỚC khi vào transaction."""
        ...

    async def accrue_for_sale(
        self,
        *,
        customer_id: uuid.UUID,
        sale_id: uuid.UUID,
        store_id: str,
        amount: Money,
        occurred_at: datetime,
    ) -> PointsAccrual:
        """Ghi `point_ledger_local` + outbox `PointsEarned` trong transaction đang mở."""
        ...

    async def reverse_for_return(
        self,
        *,
        customer_id: uuid.UUID,
        return_sale_id: uuid.UUID,
        original_sale_id: uuid.UUID,
        store_id: str,
        earned_points: int,
        original_total: Money,
        returned_amount: Money,
        occurred_at: datetime,
    ) -> PointsAccrual:
        """Dòng ledger ÂM. Không sửa dòng cũ — sự kiện bất biến (nguyên tắc #3)."""
        ...
