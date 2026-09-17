"""Cửa duy nhất `pos` được phép đi qua — ADR-004.

Đây cũng là **đường cắt microservice tương lai**: khi cần tách, chỉ đổi thân các phương
thức ở đây từ gọi hàm sang gọi HTTP. Nếu ranh giới rò rỉ ở chỗ khác, việc tách biến thành
viết lại.

`LoyaltyService` hiện thực `edge.pos.application.ports.LoyaltyPort`. Cố ý KHÔNG kế thừa
Protocol đó: kế thừa sẽ tạo cạnh import `loyalty` → `pos`, đúng thứ `import-linter` cấm.
Việc tuân thủ giao diện được mypy kiểm ở **điểm sử dụng** (tầng route truyền instance này
vào `place_sale`), nên sai lệch vẫn bị bắt lúc CI chứ không đợi tới runtime.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from edge.loyalty.domain.customer import CustomerSnapshot, PointsAccrual
from edge.loyalty.domain.points import (
    EarnRule,
    balance_of,
    displayed_balance,
    lifetime_earned_of,
    points_earned_for,
    points_to_reverse,
)
from edge.loyalty.domain.tier import TierRule, resolve_tier, tier_discount_pct
from shared.events import PointsPayload
from shared.types import Money, PointReason, Tier, new_event_id

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from edge.loyalty.adapters.postgres import PostgresCustomers, PostgresLedger
    from shared.outbox import OutboxPublisher

__all__ = ["CustomerSnapshot", "LoyaltyService", "PointsAccrual", "build_loyalty_service"]


class LoyaltyService:
    """Tích điểm, hoàn điểm, tra khách — phía cửa hàng.

    Quy tắc (`tier_rules`, `earn_rule`) được nạp sẵn lúc khởi tạo thay vì tra trong từng
    lời gọi. Hai lý do:
      - `tier_discount_pct()` trong `LoyaltyPort` là **đồng bộ**: `pos` cần % giảm giá để
        tính tiền, và không nên phải `await` một truy vấn cho một bảng 4 dòng.
      - Bộ quy tắc phải **cố định trong suốt một đơn**. Nạp một lần loại bỏ khả năng nửa
        đơn áp quy tắc cũ, nửa đơn áp quy tắc mới vừa đồng bộ về.
    """

    def __init__(
        self,
        *,
        ledger: PostgresLedger,
        customers: PostgresCustomers,
        events: OutboxPublisher,
        store_id: str,
        tier_rules: Sequence[TierRule],
        earn_rule: EarnRule,
    ) -> None:
        self._ledger = ledger
        self._customers = customers
        self._events = events
        self._store_id = store_id
        self._tier_rules = tuple(tier_rules)
        self._earn_rule = earn_rule

    # ─────────────────── Tra cứu ───────────────────

    async def resolve_customer(self, *, phone_hash: str) -> CustomerSnapshot:
        """Bước 2 của thác đổ ở docs/13 §4 — bản sao cục bộ.

        Bước 1 (Redis) và bước 3 (gọi trung tâm, timeout 500ms) nối vào ở tuần 2 ngày 9.
        Không tra được → `anonymous()`, KHÔNG ném lỗi: hợp đồng của port là "luôn trả về
        một snapshot", vì tra khách không bao giờ được chặn việc bán hàng.
        """
        return await self._customers.by_phone_hash(phone_hash) or CustomerSnapshot.anonymous()

    async def snapshot_for(self, customer_id: uuid.UUID) -> CustomerSnapshot:
        """Ảnh chụp khách theo `customer_id` — dùng khi UI đã biết khách là ai.

        Số dư hiển thị cộng thêm các dòng ledger cục bộ CHƯA đồng bộ, để khách không thấy
        điểm tụt lùi ngay sau khi mua (docs/03 §4.3).
        """
        snapshot = await self._customers.by_id(customer_id)
        if snapshot is None:
            return CustomerSnapshot.anonymous()
        pending = await self._ledger.unsynced_delta(customer_id)
        return CustomerSnapshot(
            customer_id=snapshot.customer_id,
            tier=snapshot.tier,
            balance=displayed_balance(central_balance=snapshot.balance, unsynced_deltas=[pending]),
            lifetime_earned=snapshot.lifetime_earned,
        )

    def tier_discount_pct(self, tier: Tier) -> int:
        """Thuần, đồng bộ. Hạng lạ → 0% (xem `edge.loyalty.domain.tier`)."""
        return tier_discount_pct(tier, self._tier_rules)

    async def recompute_tier(self, customer_id: uuid.UUID) -> TierRule:
        """Xếp hạng lại từ ledger cục bộ — AT-09.

        ⚠️ Dùng `lifetime_earned_of()`, KHÔNG dùng `balance_of()` (ràng buộc #6). Hai hàm
        cùng trả `int`, nên chỉ có tên và test mới ngăn được việc gọi nhầm.
        """
        entries = await self._ledger.entries_for(customer_id)
        return resolve_tier(lifetime_earned_of(entries), self._tier_rules)

    async def local_balance(self, customer_id: uuid.UUID) -> int:
        """Số dư suy từ ledger cục bộ. Có thể âm (`RETURN_ALLOW_NEGATIVE_BALANCE=true`)."""
        return balance_of(await self._ledger.entries_for(customer_id))

    # ─────────────────── Ghi ───────────────────

    async def accrue_for_sale(
        self,
        *,
        customer_id: uuid.UUID,
        sale_id: uuid.UUID,
        store_id: str,
        amount: Money,
        occurred_at: datetime,
    ) -> PointsAccrual:
        """Tích điểm cho một đơn — ghi ledger + outbox trong transaction đang mở.

        `amount` là `sale.total` (tiền khách thực trả), không phải `subtotal`.
        """
        delta = points_earned_for(amount, self._earn_rule)
        if delta == 0:
            # Đơn quá nhỏ để ra điểm. Không ghi dòng ledger delta = 0: nó vô nghĩa về
            # nghiệp vụ, vi phạm `CHECK (delta <> 0)`, và vẫn tốn một lượt đồng bộ.
            return PointsAccrual(event_id=uuid.UUID(int=0), delta=0)

        return await self._append(
            customer_id=customer_id,
            sale_id=sale_id,
            store_id=store_id,
            delta=delta,
            reason="EARN",
            occurred_at=occurred_at,
        )

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
        """Hoàn điểm khi trả hàng — FR-L08, AT-06.

        Ghi dòng ledger ÂM mới. **Không** sửa dòng cũ: ledger là append-only và lịch sử
        biến động điểm phải tra cứu được để xử lý khiếu nại (FR-L07).

        `original_sale_id` đi vào `metadata` của sự kiện chứ không vào cột `sale_id`: cột
        đó trỏ giao dịch **sinh ra** dòng ledger này, tức giao dịch trả hàng.
        """
        delta = points_to_reverse(
            earned_points=earned_points,
            sale_total=original_total,
            returned_amount=returned_amount,
        )
        if delta == 0:
            return PointsAccrual(event_id=uuid.UUID(int=0), delta=0)

        return await self._append(
            customer_id=customer_id,
            sale_id=return_sale_id,
            store_id=store_id,
            delta=-delta,
            reason="RETURN",
            occurred_at=occurred_at,
            metadata={"original_sale_id": str(original_sale_id)},
        )

    async def _append(
        self,
        *,
        customer_id: uuid.UUID,
        sale_id: uuid.UUID | None,
        store_id: str,
        delta: int,
        reason: PointReason,
        occurred_at: datetime,
        metadata: dict[str, object] | None = None,
    ) -> PointsAccrual:
        """Ghi ledger + outbox với CÙNG một `event_id` — docs/12 §3.3.

        Sinh ID ở đây, trước cả hai lần ghi. Nếu để DB sinh, ta chỉ biết giá trị sau
        `RETURNING` và hai đường ghi sẽ dễ lệch nhau khi có người thêm nhánh mới.
        """
        event_id = new_event_id()
        await self._ledger.append(
            event_id=event_id,
            customer_id=customer_id,
            store_id=store_id,
            sale_id=sale_id,
            delta=delta,
            reason=reason,
            occurred_at=occurred_at,
        )
        await self._events.publish(
            event_type="PointsEarned" if delta > 0 else "PointsReturned",
            occurred_at=occurred_at,
            event_id=event_id,
            payload=PointsPayload(
                customer_id=customer_id,
                sale_id=sale_id,
                delta=delta,
                reason=reason,
                metadata=metadata or {},
            ),
        )
        return PointsAccrual(event_id=event_id, delta=delta)


async def build_loyalty_service(
    session: AsyncSession, *, store_id: str, at: datetime
) -> LoyaltyService:
    """Dựng `LoyaltyService` trên một session đang có transaction.

    Nhận `AsyncSession` chứ không nhận sẵn adapter: **gọi viên không được biết loyalty lưu
    dữ liệu ở đâu.** Nếu tầng route phải tự `import edge.loyalty.adapters.postgres` để dựng
    tham số, thì ranh giới đã rò rỉ đúng theo cách ADR-004 cảnh báo — và khi tách
    microservice sau này, mọi chỗ gọi đều phải sửa. `import-linter` bắt được việc đó.

    `at` là `occurred_at` của giao dịch, KHÔNG phải `now()`: quy tắc tích điểm áp theo thời
    điểm mở đơn (B02 E12, L05), và hai mốc khác nhau khi đơn được chốt lại sau sự cố.
    """
    from edge.loyalty.adapters.postgres import (
        PostgresCustomers,
        PostgresLedger,
        PostgresRuleCache,
    )
    from shared.outbox import OutboxPublisher

    rules = PostgresRuleCache(session)
    return LoyaltyService(
        ledger=PostgresLedger(session),
        customers=PostgresCustomers(session),
        events=OutboxPublisher(session, store_id=store_id),
        store_id=store_id,
        tier_rules=await rules.tier_rules(),
        earn_rule=await rules.earn_rule(at),
    )
