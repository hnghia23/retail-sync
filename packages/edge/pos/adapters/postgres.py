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
from edge.pos.application.ports import OpenShift, SaleToPersist, ShiftToClose

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime

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
    """Hiện thực `ShiftPort` (đọc ca khi chốt đơn) và `ShiftLifecyclePort` (mở/đóng ca)."""

    # `FOR SHARE`: đơn đang chốt giữ khóa chia sẻ trên ca từ lúc ĐỌC ca tới lúc commit. Nhiều
    # đơn cùng lúc không chặn nhau (khóa chia sẻ tương thích nhau), nhưng đóng ca
    # (`FOR UPDATE`) phải đợi chúng xong — và đơn đến SAU khi ca đã khóa để đóng sẽ đợi, rồi
    # thấy `status = 'CLOSED'` và bị từ chối `409`.
    #
    # Khóa ngoại `sale → shift` (FOR KEY SHARE, cũng xung đột với FOR UPDATE) chỉ có hiệu lực
    # từ lúc dòng `sale` được CHÈN. Khe hở là đoạn giữa đọc ca và chèn đơn: đóng ca lọt vào
    # đó sẽ cộng tiền, commit, rồi đơn mới chèn vào một ca đã đóng — không có trong
    # `expected_cash`. `test_close_waits_for_a_sale_that_is_committing` dừng đúng trong khe đó.
    _OPEN_SHIFT = text(
        """
        SELECT shift_id, store_id, business_date
        FROM shift
        WHERE shift_id = :shift_id AND status = 'OPEN'
        FOR SHARE
        """
    )

    _OPEN = text(
        """
        INSERT INTO shift (shift_id, store_id, business_date, opened_by_employee_id,
                           opening_cash, opened_at)
        VALUES (:shift_id, :store_id, :business_date, :employee_id, :opening_cash, :opened_at)
        ON CONFLICT (store_id) WHERE status = 'OPEN' DO NOTHING
        RETURNING shift_id
        """
    )

    _LOCK_FOR_CLOSE = text(
        """
        SELECT shift_id, store_id, business_date, opened_by_employee_id, opened_at, opening_cash
        FROM shift
        WHERE shift_id = :shift_id AND store_id = :store_id AND status = 'OPEN'
        FOR UPDATE
        """
    )

    # Phần TIỀN MẶT của từng đơn (`sale_payment.amount`), không phải tiền khách đưa. Đơn trả
    # hàng có số âm nên tự trừ đi — tiền mặt hoàn cho khách đúng là tiền rời két.
    _CASH_COLLECTED = text(
        """
        SELECT COALESCE(sum(p.amount), 0)
        FROM sale_payment p JOIN sale s ON s.sale_id = p.sale_id
        WHERE s.shift_id = :shift_id AND p.method = 'CASH'
        """
    )

    _MARK_CLOSED = text(
        """
        UPDATE shift SET status = 'CLOSED', closed_at = :closed_at,
               closed_by_employee_id = :closed_by, expected_cash = :expected_cash,
               counted_cash = :counted_cash, variance = :variance, variance_note = :variance_note
        WHERE shift_id = :shift_id
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
        """Chèn trong MỘT câu lệnh có điều kiện: hai quầy cùng bấm mở ca thì một bên nhận
        `False`, không có bên nào nổ unique violation giữa chừng transaction."""
        row = (
            await self._session.execute(
                self._OPEN,
                {
                    "shift_id": shift_id,
                    "store_id": store_id,
                    "business_date": business_date,
                    "employee_id": employee_id,
                    "opening_cash": opening_cash,
                    "opened_at": opened_at,
                },
            )
        ).one_or_none()
        return row is not None

    async def lock_open_for_close(self, shift_id: uuid.UUID, store_id: str) -> ShiftToClose | None:
        row = (
            await self._session.execute(
                self._LOCK_FOR_CLOSE, {"shift_id": shift_id, "store_id": store_id}
            )
        ).one_or_none()
        if row is None:
            return None
        return ShiftToClose(
            shift_id=row.shift_id,
            store_id=row.store_id,
            business_date=row.business_date,
            opened_by_employee_id=row.opened_by_employee_id,
            opened_at=row.opened_at,
            opening_cash=row.opening_cash,
        )

    async def cash_collected(self, shift_id: uuid.UUID) -> Money:
        return int(
            (await self._session.execute(self._CASH_COLLECTED, {"shift_id": shift_id})).scalar_one()
        )

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
    ) -> None:
        await self._session.execute(
            self._MARK_CLOSED,
            {
                "shift_id": shift_id,
                "closed_by": closed_by_employee_id,
                "closed_at": closed_at,
                "expected_cash": expected_cash,
                "counted_cash": counted_cash,
                "variance": variance,
                "variance_note": variance_note,
            },
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
