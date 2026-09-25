"""Đặc tả từng bảng bronze — MỘT chỗ khai báo cả ba: SQL nguồn, kiểu Parquet, biểu thức nạp.

Ba thứ đó mà khai ba nơi thì sẽ lệch: thêm một cột ở SQL mà quên ở Parquet là mất cột âm
thầm. `test_pipeline.py` kiểm mọi cột ở đây đều có trong bảng `bronze_*` tương ứng (ddl/).

## Hai loại bảng

- `incremental`: trích theo cửa sổ `[start, end)` trên cột watermark, mép cuối không vượt
  `extract_horizon()` (docs/17 §4 bẫy 1). Watermark do trigger đặt ở mọi lần ghi (bẫy 3), nên
  một dòng bị UPDATE xuất hiện lại ở cửa sổ sau như một PHIÊN BẢN mới — staging lấy bản mới nhất.
- `snapshot`: master data nhỏ (< 10 nghìn dòng), chụp nguyên bảng mỗi ngày một file.

UUID đi qua Parquet dạng chuỗi và được `toUUID()` lúc nạp: kiểu UUID của Parquet không phải
thứ mọi bản ClickHouse đọc giống nhau, còn chuỗi thì chắc chắn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pyarrow as pa

__all__ = ["INCREMENTAL", "SNAPSHOT", "TABLES", "Column", "TableSpec"]

UTC_TS = pa.timestamp("us", tz="UTC")


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    #: Biểu thức SELECT phía Postgres (đã đặt alias bảng nếu cần).
    pg: str
    arrow: pa.DataType
    #: Biểu thức phía ClickHouse khi đọc từ `s3()`; mặc định là tên cột.
    ch: str | None = None

    def ch_expr(self) -> str:
        return self.ch or self.name


def _uuid(name: str, pg: str | None = None, *, nullable: bool = False) -> Column:
    fn = "toUUIDOrNull" if nullable else "toUUID"
    return Column(name, pg or f"{name}::text", pa.string(), f"{fn}({name})")


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    kind: Literal["incremental", "snapshot"]
    columns: tuple[Column, ...]
    #: FROM ... (kèm JOIN nếu có). WHERE cửa sổ và ORDER BY do bộ trích xuất thêm vào.
    source: str
    #: Cột watermark (đã có alias) — chỉ với `incremental`.
    watermark: str = ""
    #: ORDER BY tất định: cùng dữ liệu → cùng thứ tự dòng → cùng bytes Parquet → cùng SHA-256.
    order_by: str = ""

    @property
    def bronze(self) -> str:
        return f"bronze_{self.name}"

    def schema(self) -> pa.Schema:
        return pa.schema([pa.field(c.name, c.arrow) for c in self.columns])

    def select_sql(self) -> str:
        cols = ", ".join(f"{c.pg} AS {c.name}" for c in self.columns)
        where = f" WHERE {self.watermark} >= $1 AND {self.watermark} < $2" if self.watermark else ""
        return f"SELECT {cols} FROM {self.source}{where} ORDER BY {self.order_by}"


INCREMENTAL: tuple[TableSpec, ...] = (
    TableSpec(
        name="sale",
        kind="incremental",
        source="sale_replica s",
        watermark="s.recorded_at",
        order_by="s.sale_id",
        columns=(
            _uuid("sale_id", "s.sale_id::text"),
            Column("store_id", "s.store_id", pa.string()),
            _uuid("shift_id", "s.shift_id::text", nullable=True),
            Column("business_date", "s.business_date", pa.date32()),
            Column("employee_id", "s.employee_id", pa.string()),
            _uuid("customer_id", "s.customer_id::text", nullable=True),
            Column("occurred_at", "s.occurred_at", UTC_TS),
            Column("recorded_at", "s.recorded_at", UTC_TS),
            Column("subtotal", "s.subtotal", pa.int64()),
            Column("discount_tier", "s.discount_tier", pa.int64()),
            Column("discount_promo", "s.discount_promo", pa.int64()),
            Column("total", "s.total", pa.int64()),
            Column("tendered_amount", "s.tendered_amount", pa.int64()),
            Column("change_amount", "s.change_amount", pa.int64()),
            Column("promotion_id", "s.promotion_id", pa.string()),
            Column("authorized_by_employee_id", "s.authorized_by_employee_id", pa.string()),
            Column("status", "s.status", pa.string()),
            _uuid("voided_by_sale_id", "s.voided_by_sale_id::text", nullable=True),
            _uuid("original_sale_id", "s.original_sale_id::text", nullable=True),
        ),
    ),
    # Dòng con đi cùng cửa sổ với CHA: ghi trong cùng SAVEPOINT ở trung tâm, và mang
    # `recorded_at` của cha để staging chọn đúng phiên bản.
    TableSpec(
        name="sale_line",
        kind="incremental",
        source="sale_line_replica l JOIN sale_replica s ON s.sale_id = l.sale_id",
        watermark="s.recorded_at",
        order_by="l.sale_id, l.line_no",
        columns=(
            _uuid("sale_id", "l.sale_id::text"),
            Column("line_no", "l.line_no", pa.int32()),
            Column("product_id", "l.product_id", pa.string()),
            Column("quantity", "l.quantity", pa.int32()),
            Column("unit_price", "l.unit_price", pa.int64()),
            Column("line_total", "l.line_total", pa.int64()),
            _uuid("original_sale_id", "l.original_sale_id::text", nullable=True),
            Column("original_sale_line_no", "l.original_sale_line_no", pa.int32()),
            Column("recorded_at", "s.recorded_at", UTC_TS),
        ),
    ),
    TableSpec(
        name="sale_payment",
        kind="incremental",
        source="sale_payment_replica p JOIN sale_replica s ON s.sale_id = p.sale_id",
        watermark="s.recorded_at",
        order_by="p.sale_id, p.seq",
        columns=(
            _uuid("sale_id", "p.sale_id::text"),
            Column("seq", "p.seq", pa.int32()),
            Column("method", "p.method", pa.string()),
            Column("amount", "p.amount", pa.int64()),
            Column("reference", "p.reference", pa.string()),
            Column("recorded_at", "s.recorded_at", UTC_TS),
        ),
    ),
    TableSpec(
        name="point_ledger",
        kind="incremental",
        source="point_ledger p",
        watermark="p.recorded_at",
        order_by="p.event_id",
        columns=(
            _uuid("event_id", "p.event_id::text"),
            _uuid("customer_id", "p.customer_id::text"),
            Column("store_id", "p.store_id", pa.string()),
            _uuid("sale_id", "p.sale_id::text", nullable=True),
            Column("delta", "p.delta", pa.int32()),
            Column("reason", "p.reason", pa.string()),
            Column("occurred_at", "p.occurred_at", UTC_TS),
            Column("recorded_at", "p.recorded_at", UTC_TS),
        ),
    ),
    TableSpec(
        name="shift",
        kind="incremental",
        source="shift_replica h",
        watermark="h.recorded_at",
        order_by="h.shift_id",
        columns=(
            _uuid("shift_id", "h.shift_id::text"),
            Column("store_id", "h.store_id", pa.string()),
            Column("business_date", "h.business_date", pa.date32()),
            Column("opened_by_employee_id", "h.opened_by_employee_id", pa.string()),
            Column("closed_by_employee_id", "h.closed_by_employee_id", pa.string()),
            Column("opened_at", "h.opened_at", UTC_TS),
            Column("closed_at", "h.closed_at", UTC_TS),
            Column("opening_cash", "h.opening_cash", pa.int64()),
            Column("expected_cash", "h.expected_cash", pa.int64()),
            Column("counted_cash", "h.counted_cash", pa.int64()),
            Column("variance", "h.variance", pa.int64()),
            Column("recorded_at", "h.recorded_at", UTC_TS),
        ),
    ),
    # Ràng buộc #10: KHÔNG phone_hash, phone_enc, name_enc — không PII dưới dạng nào.
    TableSpec(
        name="customer",
        kind="incremental",
        source="customer c",
        watermark="c.recorded_at",
        order_by="c.customer_id",
        columns=(
            _uuid("customer_id", "c.customer_id::text"),
            Column("joined_at", "c.joined_at", UTC_TS),
            Column("status", "c.status", pa.string()),
            _uuid("merged_into", "c.merged_into::text", nullable=True),
            Column("recorded_at", "c.recorded_at", UTC_TS),
        ),
    ),
    TableSpec(
        name="point_balance",
        kind="incremental",
        source="point_balance b",
        watermark="b.updated_at",
        order_by="b.customer_id",
        columns=(
            _uuid("customer_id", "b.customer_id::text"),
            Column("balance", "b.balance", pa.int32()),
            Column("lifetime_earned", "b.lifetime_earned", pa.int32()),
            Column("tier", "b.tier", pa.string()),
            Column("last_event_at", "b.last_event_at", UTC_TS),
            Column("updated_at", "b.updated_at", UTC_TS),
        ),
    ),
)

SNAPSHOT: tuple[TableSpec, ...] = (
    TableSpec(
        name="region",
        kind="snapshot",
        source="region",
        order_by="region_id",
        columns=(
            Column("region_id", "region_id", pa.string()),
            Column("name", "name", pa.string()),
            Column("parent_region_id", "parent_region_id", pa.string()),
        ),
    ),
    TableSpec(
        name="store",
        kind="snapshot",
        source="store",
        order_by="store_id",
        columns=(
            Column("store_id", "store_id", pa.string()),
            Column("name", "name", pa.string()),
            Column("city", "city", pa.string()),
            Column("region_id", "region_id", pa.string()),
            Column("status", "status", pa.string()),
            Column("opened_at", "opened_at", UTC_TS),
            Column("closed_at", "closed_at", UTC_TS),
        ),
    ),
    TableSpec(
        name="category",
        kind="snapshot",
        source="category",
        order_by="category_id",
        columns=(
            Column("category_id", "category_id", pa.string()),
            Column("name", "name", pa.string()),
            Column("parent_category_id", "parent_category_id", pa.string()),
        ),
    ),
    TableSpec(
        name="product",
        kind="snapshot",
        source="product",
        order_by="product_id",
        columns=(
            Column("product_id", "product_id", pa.string()),
            Column("sku", "sku", pa.string()),
            Column("barcode", "barcode", pa.string()),
            Column("name", "name", pa.string()),
            Column("category_id", "category_id", pa.string()),
            Column("unit_price", "unit_price", pa.int64()),
            Column("is_sellable", "is_sellable", pa.bool_()),
        ),
    ),
    # Không `password_hash` — dữ liệu xác thực không có lý do nào để vào kho phân tích.
    TableSpec(
        name="employee",
        kind="snapshot",
        source="employee",
        order_by="employee_id",
        columns=(
            Column("employee_id", "employee_id", pa.string()),
            Column("store_id", "store_id", pa.string()),
            Column("name", "name", pa.string()),
            Column("role", "role", pa.string()),
            Column("status", "status", pa.string()),
        ),
    ),
    TableSpec(
        name="tier_rule",
        kind="snapshot",
        source="tier_rule",
        order_by="min_lifetime_points",
        columns=(
            Column("tier", "tier", pa.string()),
            Column("min_lifetime_points", "min_lifetime_points", pa.int32()),
            Column("discount_pct", "discount_pct", pa.int32()),
        ),
    ),
)

TABLES: tuple[TableSpec, ...] = INCREMENTAL + SNAPSHOT
