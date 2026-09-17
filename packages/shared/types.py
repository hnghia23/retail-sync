"""Kiểu dữ liệu nền — quy ước ở CLAUDE.md §Quy ước.

Hai quy ước dễ sai nhất, đóng thành kiểu/hàm để máy bắt lỗi thay vì người:
  - Tiền: `bigint` đơn vị đồng. KHÔNG BAO GIỜ float.
  - Thời gian: `timestamptz` UTC, tách `occurred_at` (cửa hàng) / `recorded_at` (trung tâm).

`business_date` cố ý KHÔNG có hàm suy ra ở đây: nó do người mở ca xác định và được ghi vào
`shift.business_date` (docs/05 §3.2, G5). Một hàm `business_date_of(now())` sẽ mời gọi việc
suy ra lại ở chỗ khác, và ca kéo qua nửa đêm sẽ rơi sai ngày.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field

# Đơn vị: đồng.
Money = Annotated[int, Field(description="Số tiền, đơn vị đồng (không dùng float)")]

# ── Enum nghiệp vụ (khớp cột `text` trong docs/05-data-model.md) ─────────────
PaymentMethod = Literal["CASH", "CARD", "EWALLET"]
SaleStatus = Literal["COMPLETED", "VOIDED", "RETURN"]
ShiftStatus = Literal["OPEN", "CLOSED"]
PointReason = Literal["EARN", "REDEEM", "RETURN", "EXPIRE", "ADJUST"]
Tier = Literal["BRONZE", "SILVER", "GOLD", "PLATINUM"]  # docs/15-glossary.md
Role = Literal["cashier", "manager", "region_manager", "admin"]  # docs/15-glossary.md §Vai trò


def new_event_id() -> uuid.UUID:
    """Sinh UUIDv7 ở tầng Python.

    Mặc định DDL dùng `uuidv7()` native của PG 18. Hàm này cần cho trường hợp ID phải có
    TRƯỚC khi chạm DB — cụ thể là ghi outbox: `event_id` phải nằm trong payload lẫn trong
    cột, và với sự kiện điểm thì nó còn phải trùng `point_ledger.event_id` (docs/12 §3.3).
    """
    from uuid6 import uuid7

    return uuid7()


def utcnow() -> datetime:
    """`now()` luôn aware-UTC. Ruff rule DTZ cấm `datetime.now()` trần trong repo này."""
    return datetime.now(UTC)
