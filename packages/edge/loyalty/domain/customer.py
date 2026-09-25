"""Kiểu dữ liệu đi qua ranh giới POS ↔ Loyalty.

## Vì sao chúng nằm ở `loyalty` chứ không ở `pos`

`pos` gọi `loyalty`, nên `loyalty` là bên **cung cấp** — và bên cung cấp sở hữu hợp đồng
của mình. Nếu để `pos` định nghĩa `CustomerSnapshot`, thì `loyalty` phải import `pos` để
dựng giá trị trả về, tạo cạnh phụ thuộc ngược chiều mà ADR-004 cấm (`import-linter` sẽ
chặn).

Chúng được `edge/loyalty/api.py` xuất ra ngoài; `pos` import từ đó, không import trực tiếp
từ file này.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from shared.types import Tier


@dataclass(frozen=True, slots=True)
class CustomerSnapshot:
    """Thứ `pos` cần biết về khách — KHÔNG hơn.

    Cố ý không mang tên, số điện thoại hay lịch sử: `pos` không cần chúng để tính tiền, và
    không mang theo nghĩa là không có gì để rò rỉ vào log hay span (ràng buộc #10).

    `tier_unknown=True` là trạng thái HỢP LỆ, không phải lỗi: trung tâm không tới được thì
    vẫn bán hàng với hạng mặc định (docs/13 §4), điểm đối soát sau.
    """

    customer_id: uuid.UUID | None
    tier: Tier
    balance: int
    lifetime_earned: int
    tier_unknown: bool = False

    @classmethod
    def anonymous(cls) -> CustomerSnapshot:
        """Khách vãng lai, hoặc khách không tra được. Cả hai đều bán hàng bình thường."""
        return cls(customer_id=None, tier="BRONZE", balance=0, lifetime_earned=0, tier_unknown=True)


@dataclass(frozen=True, slots=True)
class RegisteredCustomer:
    """Kết quả đăng ký khách tại quầy — FR-L02.

    `created=False`: SĐT đã có ở cửa hàng này, trả về khách cũ thay vì tạo bản trùng.
    Thu ngân gõ lại SĐT của khách quen là chuyện hằng ngày, không phải lỗi.
    """

    customer_id: uuid.UUID
    created: bool


@dataclass(frozen=True, slots=True)
class PointsAccrual:
    """Kết quả một lần ghi ledger.

    ⚠️ `event_id` do loyalty sinh và PHẢI được dùng lại làm `outbox.event_id` của sự kiện
    `PointsEarned`/`PointsReturned` (docs/12 §3.3). Sinh ID mới ở bước chuyển tiếp là phá
    idempotency: trung tâm sẽ không nhận ra bản lặp và cộng điểm hai lần.
    """

    event_id: uuid.UUID
    delta: int
