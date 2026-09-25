"""Adapter Postgres của module Loyalty — ledger cục bộ, khách cục bộ, quy tắc đã cache.

Ranh giới (ADR-004 quy tắc 2): các truy vấn ở đây **chỉ** chạm bảng của loyalty
(`point_ledger_local`, `customer_local`) và bản sao quy tắc (`tier_rule_cache`,
`earn_rule_cache`). Không `SELECT` từ `sale` hay `sale_line` — nếu cần số liệu đơn hàng,
`pos` phải truyền vào qua tham số.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import text

from edge.loyalty.domain.customer import CustomerSnapshot
from edge.loyalty.domain.points import EarnRule, LedgerEntry
from edge.loyalty.domain.tier import DEFAULT_TIER_RULES, TierRule

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from shared.types import PointReason


class PostgresLedger:
    """Ghi và đọc `point_ledger_local`. **Append-only** — không có phương thức UPDATE."""

    _APPEND = text(
        """
        INSERT INTO point_ledger_local (event_id, customer_id, store_id, sale_id, delta,
                                        reason, occurred_at)
        VALUES (:event_id, :customer_id, :store_id, :sale_id, :delta, :reason, :occurred_at)
        """
    )

    _ENTRIES = text(
        """
        SELECT delta, reason FROM point_ledger_local
        WHERE customer_id = :customer_id ORDER BY occurred_at
        """
    )

    _UNSYNCED = text(
        """
        SELECT COALESCE(sum(delta), 0) AS pending
        FROM point_ledger_local
        WHERE customer_id = :customer_id AND synced_at IS NULL
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        event_id: uuid.UUID,
        customer_id: uuid.UUID,
        store_id: str,
        sale_id: uuid.UUID | None,
        delta: int,
        reason: PointReason,
        occurred_at: datetime,
    ) -> None:
        """`event_id` do gọi viên sinh, không để DB tự sinh.

        Lý do: cùng một `event_id` phải xuất hiện ở cả dòng ledger lẫn payload outbox
        (docs/12 §3.3). Để DEFAULT `uuidv7()` của DB sinh thì ta không biết giá trị đó cho
        tới khi `RETURNING`, và hai bên sẽ dễ lệch nhau khi ai đó thêm đường ghi mới.
        """
        await self._session.execute(
            self._APPEND,
            {
                "event_id": event_id,
                "customer_id": customer_id,
                "store_id": store_id,
                "sale_id": sale_id,
                "delta": delta,
                "reason": reason,
                "occurred_at": occurred_at,
            },
        )

    async def entries_for(self, customer_id: uuid.UUID) -> list[LedgerEntry]:
        rows = await self._session.execute(self._ENTRIES, {"customer_id": customer_id})
        return [LedgerEntry(delta=row.delta, reason=row.reason) for row in rows]

    async def unsynced_delta(self, customer_id: uuid.UUID) -> int:
        """Tổng các dòng CHƯA lên trung tâm — để số dư hiển thị không tụt lùi (docs/03 §4.3)."""
        return int(
            (await self._session.execute(self._UNSYNCED, {"customer_id": customer_id})).scalar_one()
        )


class PostgresCustomers:
    """Bước 2 của thác đổ tra cứu khách (docs/13 §4) — bản sao cục bộ.

    Bước 1 (Redis) và bước 3 (gọi trung tâm, timeout 500ms) sẽ được nối vào ở giai đoạn C (ADR-010).
    Bản cục bộ này đứng một mình vẫn đúng nghiệp vụ và **chạy được khi mất mạng hoàn toàn**
    — đúng nguyên tắc kiến trúc #1. Thiếu hai bước kia chỉ làm giảm tỷ lệ nhận diện được
    khách lạ với cửa hàng, không làm sai kết quả.
    """

    _BY_ID = text(
        """
        SELECT customer_id, tier, cached_balance, cached_lifetime_earned
        FROM customer_local WHERE customer_id = :customer_id
        """
    )

    _BY_PHONE_HASH = text(
        """
        SELECT customer_id, tier, cached_balance, cached_lifetime_earned
        FROM customer_local WHERE phone_hash = :phone_hash
        """
    )

    # Chèn-hoặc-lấy bằng HAI câu lệnh, không phải một CTE `INSERT ... UNION SELECT`.
    #
    # Hai quầy cùng đăng ký một SĐT: bên sau đợi ở `ON CONFLICT` cho tới khi bên trước
    # commit, rồi `DO NOTHING`. Nếu phần SELECT nằm trong CÙNG câu lệnh, nó dùng snapshot chụp
    # từ đầu câu lệnh — lúc bên trước chưa commit — nên không thấy dòng nào và trả về rỗng
    # (test `test_concurrent_registration_of_one_phone_creates_one_customer` bắt được đúng lỗi
    # này ở bản CTE). Ở READ COMMITTED, câu SELECT RIÊNG chụp snapshot mới và thấy dòng vừa
    # commit. Bên trước rollback thay vì commit → `ON CONFLICT` của bên sau chèn được luôn.
    _INSERT = text(
        """
        INSERT INTO customer_local (customer_id, phone_hash, created_locally)
        VALUES (:customer_id, :phone_hash, true)
        ON CONFLICT (phone_hash) DO NOTHING
        RETURNING customer_id
        """
    )

    _EXISTING = text("SELECT customer_id FROM customer_local WHERE phone_hash = :phone_hash")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(self, *, customer_id: uuid.UUID, phone_hash: str) -> tuple[uuid.UUID, bool]:
        """`(customer_id, created)`. Không lưu SĐT hay tên dạng đọc được (ràng buộc #10).

        `phone_enc`/`name_enc` để NULL ở giai đoạn A: chưa có khóa mã hóa, và không thu thập
        PII khi chưa bảo vệ được nó. Luồng dữ liệu chỉ cần `phone_hash` (tra cứu, dò trùng).
        """
        inserted = (
            await self._session.execute(
                self._INSERT, {"customer_id": customer_id, "phone_hash": phone_hash}
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return inserted, True
        existing = (
            await self._session.execute(self._EXISTING, {"phone_hash": phone_hash})
        ).scalar_one()
        return existing, False

    async def by_id(self, customer_id: uuid.UUID) -> CustomerSnapshot | None:
        row = (await self._session.execute(self._BY_ID, {"customer_id": customer_id})).one_or_none()
        return self._to_snapshot(row)

    async def by_phone_hash(self, phone_hash: str) -> CustomerSnapshot | None:
        row = (
            await self._session.execute(self._BY_PHONE_HASH, {"phone_hash": phone_hash})
        ).one_or_none()
        return self._to_snapshot(row)

    @staticmethod
    def _to_snapshot(row: object) -> CustomerSnapshot | None:
        if row is None:
            return None
        return CustomerSnapshot(
            customer_id=row.customer_id,  # type: ignore[attr-defined]
            tier=row.tier,  # type: ignore[attr-defined]
            balance=row.cached_balance,  # type: ignore[attr-defined]
            lifetime_earned=row.cached_lifetime_earned,  # type: ignore[attr-defined]
        )


class PostgresRuleCache:
    """Đọc `tier_rule_cache` / `earn_rule_cache`.

    ⚠️ Cache RỖNG là trạng thái hợp lệ, không phải lỗi: cửa hàng mới lắp đặt, hoặc chưa
    đồng bộ được lần nào. Cả hai hàm đều trả về mặc định thay vì ném lỗi — cửa hàng phải
    bán được hàng (nguyên tắc kiến trúc #1). Bán hụt chiết khấu thì bù được; chặn quầy thì
    không.
    """

    _TIER_RULES = text(
        "SELECT tier, min_lifetime_points, discount_pct FROM tier_rule_cache "
        "ORDER BY min_lifetime_points"
    )

    _EARN_RULE = text(
        """
        SELECT vnd_per_point, multiplier FROM earn_rule_cache
        WHERE valid_from <= :at AND (valid_to IS NULL OR valid_to > :at)
        ORDER BY valid_from DESC LIMIT 1
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def tier_rules(self) -> tuple[TierRule, ...]:
        rows = (await self._session.execute(self._TIER_RULES)).all()
        if not rows:
            return DEFAULT_TIER_RULES
        return tuple(
            TierRule(
                tier=row.tier,
                min_lifetime_points=row.min_lifetime_points,
                discount_pct=row.discount_pct,
            )
            for row in rows
        )

    async def earn_rule(self, at: datetime) -> EarnRule:
        """Quy tắc CÓ HIỆU LỰC tại `at`, không phải quy tắc mới nhất.

        Giao dịch áp quy tắc tại thời điểm mở đơn (B02 E12, L05). Truyền `occurred_at` của
        đơn vào đây, đừng truyền `now()` — chúng khác nhau khi đơn được chốt lại sau sự cố.
        """
        row = (await self._session.execute(self._EARN_RULE, {"at": at})).one_or_none()
        if row is None:
            return EarnRule()
        return EarnRule(vnd_per_point=row.vnd_per_point, multiplier=Decimal(str(row.multiplier)))
