"""Adapter Postgres cho các port của `pos` — tầng DUY NHẤT biết SQL.

Mỗi adapter giữ **cùng một** `AsyncSession` do tầng route mở. Đó là cách "lưu đơn + tích
điểm + ghi outbox" thành một hành động nguyên tử: không adapter nào tự mở transaction, tự
`commit()`, hay tự `rollback()`.

`EventPublisherPort` được hiện thực bởi `shared.outbox.OutboxPublisher` — nó không nằm ở
đây vì `loyalty` cũng ghi outbox, và không module nào trong hai được sở hữu nó.

Dùng SQL thô qua `text()` thay vì ORM: schema có CHECK constraint, constraint trigger hoãn
và cột sinh bởi `uuidv7()` — mapping ORM chỉ thêm một tầng phải đồng bộ tay với migration,
đổi lại không được gì ở đây (không có quan hệ lười, không có identity map cần thiết).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import text

from edge.pos.application.login import EmployeeAccount
from edge.pos.application.ports import OpenShift, SaleToPersist

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from shared.types import Money


class ProductLookup(TypedDict):
    """Kết quả `by_barcode` — dict thô nhưng kiểu từng trường chính xác.

    Dùng `TypedDict` thay vì `dict[str, object]`: route và test đọc lại các trường này
    (`synced_hours_ago < 1`, so sánh `unit_price`...), và `object` xóa hết thông tin kiểu
    ngay khi ra khỏi hàm.
    """

    product_id: str
    sku: str
    barcode: str | None
    name: str
    unit_price: Money
    synced_hours_ago: float


class PostgresProductCatalog:
    """Đọc `product_cache`. Hiện thực `ProductCatalogPort`."""

    _PRICES = text(
        """
        SELECT product_id, unit_price
        FROM product_cache
        WHERE product_id = ANY(:product_ids) AND is_sellable
        """
    )

    _BY_BARCODE = text(
        """
        SELECT product_id, sku, barcode, name, unit_price,
               EXTRACT(EPOCH FROM now() - synced_at) / 3600 AS synced_hours_ago
        FROM product_cache
        WHERE barcode = :barcode AND is_sellable
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def prices_for(self, product_ids: Sequence[str]) -> dict[str, Money]:
        """Một truy vấn cho cả giỏ hàng, không phải một truy vấn mỗi dòng.

        `is_sellable = false` khiến sản phẩm vắng mặt trong kết quả, y như khi nó không
        tồn tại — G7: hàng ngừng kinh doanh vẫn bán được phần tồn, nên cờ này do trung tâm
        quyết định, còn cửa hàng chỉ tuân theo.
        """
        if not product_ids:
            return {}
        rows = await self._session.execute(self._PRICES, {"product_ids": list(set(product_ids))})
        return {row.product_id: row.unit_price for row in rows}

    async def by_barcode(self, barcode: str) -> ProductLookup | None:
        """FR-P03 — quét mã vạch. Có unique index trên `barcode` nên đây là index scan.

        Trả kèm `synced_hours_ago` để UI cảnh báo khi bản sao master data quá cũ: cửa hàng
        offline lâu vẫn bán được, nhưng người bán cần biết giá có thể đã lỗi thời (B02 E03).
        """
        row = (await self._session.execute(self._BY_BARCODE, {"barcode": barcode})).one_or_none()
        if row is None:
            return None
        return {
            "product_id": row.product_id,
            "sku": row.sku,
            "barcode": row.barcode,
            "name": row.name,
            "unit_price": row.unit_price,
            "synced_hours_ago": float(row.synced_hours_ago),
        }


class PostgresShifts:
    """Hiện thực `ShiftPort`."""

    _OPEN_SHIFT = text(
        """
        SELECT shift_id, store_id, business_date
        FROM shift
        WHERE shift_id = :shift_id AND status = 'OPEN'
        """
    )

    _ANY_OPEN_SHIFT = text(
        """
        SELECT shift_id, store_id, business_date
        FROM shift
        WHERE store_id = :store_id AND status = 'OPEN'
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_open_shift(self, shift_id: uuid.UUID) -> OpenShift | None:
        """Ca đã đóng và ca không tồn tại trả về cùng một kết quả: `None`.

        Cả hai đều là `409` với thu ngân (docs/13 §3), và phân biệt chúng sẽ để lộ việc
        một `shift_id` có tồn tại hay không cho người gọi không có quyền.
        """
        row = (await self._session.execute(self._OPEN_SHIFT, {"shift_id": shift_id})).one_or_none()
        if row is None:
            return None
        return OpenShift(
            shift_id=row.shift_id, store_id=row.store_id, business_date=row.business_date
        )

    async def find_open_shift(self, store_id: str) -> OpenShift | None:
        """Ca đang mở của cửa hàng, KHÔNG cần biết trước `shift_id`.

        Dùng ở UI: màn hình bán hàng cần biết "có ca nào đang mở không" trước khi cho
        thu ngân thao tác. `shift_one_open_per_store_idx` (migration) đảm bảo có tối đa
        một dòng khớp, nên `.one_or_none()` không bao giờ ném lỗi nhiều dòng.
        """
        row = (
            await self._session.execute(self._ANY_OPEN_SHIFT, {"store_id": store_id})
        ).one_or_none()
        if row is None:
            return None
        return OpenShift(
            shift_id=row.shift_id, store_id=row.store_id, business_date=row.business_date
        )


class PostgresEmployees:
    """Hiện thực `EmployeeAuthPort` — đọc `employee_cache`.

    Chỉ đọc. Nhân viên được tạo/thu hồi từ trung tâm, đồng bộ xuống — cửa hàng không tự
    tạo tài khoản (khác với khách hàng, nơi C02 cho phép tạo cục bộ khi offline).
    """

    _BY_ID = text(
        """
        SELECT employee_id, role, password_hash, is_active, revoked_at
        FROM employee_cache WHERE employee_id = :employee_id
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id(self, employee_id: str) -> EmployeeAccount | None:
        row = (await self._session.execute(self._BY_ID, {"employee_id": employee_id})).one_or_none()
        if row is None:
            return None
        return EmployeeAccount(
            employee_id=row.employee_id,
            role=row.role,
            password_hash=row.password_hash,
            is_active=row.is_active,
            revoked_at=row.revoked_at,
        )


class PostgresSaleRepository:
    """Hiện thực `SaleRepositoryPort` — ghi cả ba bảng của một đơn."""

    _INSERT_SALE = text(
        """
        INSERT INTO sale (
            sale_id, store_id, shift_id, business_date, employee_id, customer_id, occurred_at,
            subtotal, discount_tier, discount_promo, total,
            tendered_amount, change_amount, promotion_id, status, original_sale_id
        ) VALUES (
            :sale_id, :store_id, :shift_id, :business_date, :employee_id, :customer_id,
            :occurred_at, :subtotal, :discount_tier, :discount_promo, :total,
            :tendered_amount, :change_amount, :promotion_id, :status, :original_sale_id
        )
        """
    )

    _INSERT_LINE = text(
        """
        INSERT INTO sale_line (
            sale_id, line_no, product_id, quantity, unit_price, line_total,
            original_sale_id, original_sale_line_no
        ) VALUES (
            :sale_id, :line_no, :product_id, :quantity, :unit_price, :line_total,
            :original_sale_id, :original_sale_line_no
        )
        """
    )

    _INSERT_PAYMENT = text(
        """
        INSERT INTO sale_payment (sale_id, seq, method, amount, reference)
        VALUES (:sale_id, :seq, :method, :amount, :reference)
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, sale: SaleToPersist) -> None:
        """Ba lệnh INSERT, một thứ tự bắt buộc: `sale` trước, rồi dòng hàng và thanh toán.

        Thứ tự do khóa ngoại quyết định. Việc tổng dòng hàng chưa khớp `subtotal` ở giữa
        chừng là bình thường — constraint trigger INV-2/INV-3 hoãn tới `COMMIT` chính là
        để cho phép trạng thái trung gian này (xem migration `0001_edge_initial`).
        """
        await self._session.execute(
            self._INSERT_SALE,
            {
                "sale_id": sale.sale_id,
                "store_id": sale.store_id,
                "shift_id": sale.shift_id,
                "business_date": sale.business_date,
                "employee_id": sale.employee_id,
                "customer_id": sale.customer_id,
                "occurred_at": sale.occurred_at,
                "subtotal": sale.subtotal,
                "discount_tier": sale.discount_tier,
                "discount_promo": sale.discount_promo,
                "total": sale.total,
                "tendered_amount": sale.tendered_amount,
                "change_amount": sale.change_amount,
                "promotion_id": sale.promotion_id,
                "status": sale.status,
                "original_sale_id": sale.original_sale_id,
            },
        )
        await self._session.execute(
            self._INSERT_LINE,
            [
                {
                    "sale_id": sale.sale_id,
                    "line_no": line.line_no,
                    "product_id": line.product_id,
                    "quantity": line.quantity,
                    "unit_price": line.unit_price,
                    "line_total": line.line_total,
                    "original_sale_id": line.original_sale_id,
                    "original_sale_line_no": line.original_sale_line_no,
                }
                for line in sale.lines
            ],
        )
        await self._session.execute(
            self._INSERT_PAYMENT,
            [
                {
                    "sale_id": sale.sale_id,
                    "seq": payment.seq,
                    "method": payment.method,
                    "amount": payment.amount,
                    "reference": payment.reference,
                }
                for payment in sale.payments
            ],
        )
