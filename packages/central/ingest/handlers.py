"""Áp một sự kiện đã xác thực vào DB trung tâm — mỗi loại một hàm.

Gọi viên (`central.ingest.service`) đã lo: xác thực cửa hàng, kiểm `schema_version`, ép
payload về đúng model, chốt chặn idempotency. Hàm ở đây chỉ còn việc GHI, và chạy trong một
SAVEPOINT riêng — ném lỗi thì chỉ sự kiện này bị hoàn tác, cả lô vẫn đi tiếp.

## Không có handler nào tự kiểm "đã xử lý chưa"

Đó là việc của `processed_event` (docs/12 §5), đã chạy trước. Một handler tự thêm
`ON CONFLICT DO NOTHING` vào bảng nghiệp vụ sẽ âm thầm nuốt cả những va chạm THẬT (hai
`event_id` khác nhau mang cùng `sale_id`) — đúng loại lỗi dữ liệu cần phải ồn ào.
Ngoại lệ duy nhất có lý do: `CustomerCreated` (xem hàm đó).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from shared.events import (
    CustomerPayload,
    EventEnvelope,
    PointsPayload,
    SaleCompletedPayload,
    ShiftClosedPayload,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from shared.config import ReturnRules

Handler = Callable[["AsyncSession", EventEnvelope, Any, "ReturnRules"], Awaitable[None]]


class PermanentEventError(ValueError):
    """Lỗi của chính sự kiện — gửi lại không bao giờ sửa được. Worker dead-letter ngay."""


class RetryableEventError(RuntimeError):
    """Lỗi của thời điểm — sự kiện đúng nhưng chưa áp được lúc này. Worker thử lại sau."""


# ═══════════════════════════ SaleCompleted — docs/12 §3.1 ═══════════════════════════

_INSERT_SALE = text(
    """
    INSERT INTO sale_replica (
        sale_id, store_id, shift_id, business_date, employee_id, customer_id, occurred_at,
        subtotal, discount_tier, discount_promo, total, tendered_amount, change_amount,
        promotion_id, authorized_by_employee_id, status
    ) VALUES (
        :sale_id, :store_id, :shift_id, :business_date, :employee_id, :customer_id, :occurred_at,
        :subtotal, :discount_tier, :discount_promo, :total, :tendered_amount, :change_amount,
        :promotion_id, :authorized_by_employee_id, 'COMPLETED'
    )
    """
)
_INSERT_LINE = text(
    """
    INSERT INTO sale_line_replica (sale_id, line_no, product_id, quantity, unit_price,
                                   line_total, original_sale_id, original_sale_line_no)
    VALUES (:sale_id, :line_no, :product_id, :quantity, :unit_price,
            :line_total, :original_sale_id, :original_sale_line_no)
    """
)
_INSERT_PAYMENT = text(
    """
    INSERT INTO sale_payment_replica (sale_id, seq, method, amount, reference)
    VALUES (:sale_id, :seq, :method, :amount, :reference)
    """
)


async def apply_sale_completed(
    session: AsyncSession, env: EventEnvelope, p: SaleCompletedPayload, _rules: ReturnRules
) -> None:
    """Ghi cả ba bảng replica từ MỘT sự kiện — không có trạng thái "có đơn mà thiếu dòng"."""
    await session.execute(
        _INSERT_SALE,
        {
            "sale_id": p.sale_id,
            "store_id": env.store_id,
            "shift_id": p.shift_id,
            "business_date": p.business_date,
            "employee_id": p.employee_id,
            "customer_id": p.customer_id,
            "occurred_at": env.occurred_at,
            "subtotal": p.subtotal,
            "discount_tier": p.discount_tier,
            "discount_promo": p.discount_promo,
            "total": p.total,
            "tendered_amount": p.tendered_amount,
            "change_amount": p.change_amount,
            "promotion_id": p.promotion_id,
            "authorized_by_employee_id": p.authorized_by_employee_id,
        },
    )
    await session.execute(
        _INSERT_LINE,
        [{"sale_id": p.sale_id, **line.model_dump()} for line in p.lines],
    )
    await session.execute(
        _INSERT_PAYMENT,
        [{"sale_id": p.sale_id, **pay.model_dump()} for pay in p.payments],
    )


# ═══════════════════════ Points* — docs/12 §3.3, ADR-002 ═══════════════════════

_INSERT_LEDGER = text(
    """
    INSERT INTO point_ledger (event_id, customer_id, store_id, sale_id, delta, reason,
                              occurred_at, metadata)
    VALUES (:event_id, :customer_id, :store_id, :sale_id, :delta, :reason,
            :occurred_at, CAST(:metadata AS jsonb))
    """
)

# Snapshot tăng dần (docs/08 §4.3): cộng dồn, không tính lại từ đầu. `ON CONFLICT DO UPDATE`
# khóa đúng một dòng khách, nên hai cửa hàng tích điểm cho cùng khách ĐỒNG THỜI vẫn ra
# tổng đúng — đó là AT-04 ở phía trung tâm, và là lý do không đọc-rồi-ghi ở tầng Python.
_UPSERT_BALANCE = text(
    """
    INSERT INTO point_balance (customer_id, balance, lifetime_earned, last_event_at)
    VALUES (:customer_id, :delta, :earned, :occurred_at)
    ON CONFLICT (customer_id) DO UPDATE SET
        balance         = point_balance.balance + EXCLUDED.balance,
        lifetime_earned = GREATEST(0, point_balance.lifetime_earned + EXCLUDED.lifetime_earned),
        last_event_at   = GREATEST(point_balance.last_event_at, EXCLUDED.last_event_at),
        updated_at      = now()
    RETURNING lifetime_earned
    """
)

# Hạng suy từ `tier_rule` CỦA TRUNG TÂM — nơi sở hữu quy tắc (docs/05 §2). Cửa hàng chỉ
# giữ bản cache. Không khớp mốc nào (bảng rỗng) → hạng nền, cùng hành vi với phía cửa hàng
# khi chưa đồng bộ được quy tắc.
_SET_TIER = text(
    """
    UPDATE point_balance SET tier = COALESCE(
        (SELECT tier FROM tier_rule
         WHERE min_lifetime_points <= :lifetime_earned
         ORDER BY min_lifetime_points DESC LIMIT 1),
        'BRONZE')
    WHERE customer_id = :customer_id
    """
)


def lifetime_contribution(delta: int, reason: str, rules: ReturnRules) -> int:
    """Phần một dòng ledger đóng góp vào `lifetime_earned` — ràng buộc #6.

    Cùng định nghĩa với `edge.loyalty.domain.points.lifetime_earned_of` (không import được
    qua ranh giới edge↔central; test `test_lifetime_rule_matches_edge` giữ hai bên khớp).
    Job đối soát (INV-4) phải tính lại đúng theo hàm này.

      - `EARN` dương: cộng.
      - `RETURN`: chỉ trừ khi `RETURN_DEMOTE_TIER_ON_RETURN=true` (mặc định false — trả hàng
        không làm tụt hạng, Q-B5).
      - `REDEEM`, `EXPIRE`, `ADJUST`: không đổi — tiêu điểm không được làm tụt hạng.
    """
    if reason == "EARN" and delta > 0:
        return delta
    if reason == "RETURN" and rules.demote_tier_on_return:
        return delta
    return 0


async def apply_points(
    session: AsyncSession, env: EventEnvelope, p: PointsPayload, rules: ReturnRules
) -> None:
    """Ledger trước, snapshot sau — trong cùng SAVEPOINT.

    Khách chưa có ở trung tâm → khóa ngoại vỡ ngay ở câu đầu → `RetryableEventError` từ tầng
    service. Đúng hành vi: `CustomerCreated` của khách đó nằm TRƯỚC trong outbox (cùng lô
    hoặc lô trước), chỉ là chưa áp được — lượt sau sẽ qua.

    `event_id` của dòng ledger CHÍNH LÀ `event_id` của envelope (docs/12 §3.3).
    """
    import json

    await session.execute(
        _INSERT_LEDGER,
        {
            "event_id": env.event_id,
            "customer_id": p.customer_id,
            "store_id": env.store_id,
            "sale_id": p.sale_id,
            "delta": p.delta,
            "reason": p.reason,
            "occurred_at": env.occurred_at,
            "metadata": json.dumps(p.metadata),
        },
    )
    lifetime = (
        await session.execute(
            _UPSERT_BALANCE,
            {
                "customer_id": p.customer_id,
                "delta": p.delta,
                "earned": lifetime_contribution(p.delta, p.reason, rules),
                "occurred_at": env.occurred_at,
            },
        )
    ).scalar_one()
    await session.execute(_SET_TIER, {"customer_id": p.customer_id, "lifetime_earned": lifetime})


# ═══════════════════════ Customer* — docs/12 §3.4, case C03 ═══════════════════════

_INSERT_CUSTOMER = text(
    """
    INSERT INTO customer (customer_id, phone_hash, phone_enc, name_enc, joined_at)
    VALUES (:customer_id, :phone_hash, :phone_enc, :name_enc, :joined_at)
    ON CONFLICT (customer_id) DO NOTHING
    """
)
_UPDATE_CUSTOMER = text(
    """
    UPDATE customer SET phone_hash = :phone_hash, phone_enc = :phone_enc, name_enc = :name_enc
    WHERE customer_id = :customer_id
    """
)
_FIND_SAME_PHONE = text(
    """
    SELECT array_agg(customer_id ORDER BY joined_at, customer_id)
    FROM customer WHERE phone_hash = :phone_hash AND status <> 'MERGED'
    """
)
_FLAG_DUPLICATE = text(
    """
    INSERT INTO customer_duplicate_candidate (phone_hash, customer_ids)
    VALUES (:phone_hash, :customer_ids)
    ON CONFLICT (phone_hash) DO UPDATE SET
        customer_ids = EXCLUDED.customer_ids, detected_at = now(), resolved_at = NULL
    """
)


def _decode_ciphertext(value: str | None, field: str) -> bytes | None:
    """`*_enc` đi qua JSON dưới dạng base64 chuẩn (docs/12 §3.4)."""
    if value is None:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PermanentEventError(f"{field} không phải base64 hợp lệ") from exc


def _customer_params(p: CustomerPayload) -> dict[str, object]:
    return {
        "customer_id": p.customer_id,
        "phone_hash": p.phone_hash,
        "phone_enc": _decode_ciphertext(p.phone_enc, "phone_enc"),
        "name_enc": _decode_ciphertext(p.name_enc, "name_enc"),
    }


async def _flag_if_duplicate(session: AsyncSession, phone_hash: str) -> None:
    """C03 — phát hiện tự động, gộp THỦ CÔNG.

    Không tự gộp: gộp sai không hoàn tác được vì ledger là append-only, và hai bản ghi
    trùng SĐT có thể là hai người thật (số điện thoại tái sử dụng, C07).
    """
    ids = (await session.execute(_FIND_SAME_PHONE, {"phone_hash": phone_hash})).scalar_one()
    if ids and len(ids) > 1:
        await session.execute(_FLAG_DUPLICATE, {"phone_hash": phone_hash, "customer_ids": ids})


async def apply_customer_created(
    session: AsyncSession, env: EventEnvelope, p: CustomerPayload, _rules: ReturnRules
) -> None:
    """`ON CONFLICT (customer_id) DO NOTHING` — ngoại lệ duy nhất của quy tắc ở docstring module.

    Khách có thể đã tồn tại ở trung tâm theo đường KHÁC chuỗi outbox này: cửa hàng tra khách
    từ trung tâm về rồi lưu cục bộ (docs/13 §4). Cùng `customer_id` thì chắc chắn là cùng
    một khách, nên bỏ qua là đúng — khác hẳn trường hợp trùng `sale_id` giữa hai sự kiện.
    """
    # `joined_at` = lúc khách đăng ký TẠI CỬA HÀNG, không phải lúc sự kiện tới trung tâm.
    # Để DEFAULT now() thì cửa hàng offline 3 ngày làm ngày gia nhập của khách lệch 3 ngày
    # trong `dim_customer` — phân tích cohort theo ngày gia nhập sai mà không ai thấy.
    await session.execute(_INSERT_CUSTOMER, {**_customer_params(p), "joined_at": env.occurred_at})
    await _flag_if_duplicate(session, p.phone_hash)


async def apply_customer_updated(
    session: AsyncSession, _env: EventEnvelope, p: CustomerPayload, _rules: ReturnRules
) -> None:
    result = await session.execute(_UPDATE_CUSTOMER, _customer_params(p))
    if getattr(result, "rowcount", 0) == 0:
        # `CustomerCreated` nằm trước trong outbox nhưng chưa áp được. Chờ nó.
        raise RetryableEventError(f"customer {p.customer_id} chưa tồn tại ở trung tâm")
    await _flag_if_duplicate(session, p.phone_hash)


# ═══════════════════════════ ShiftClosed — docs/12 §3.5 ═══════════════════════════

_INSERT_SHIFT = text(
    """
    INSERT INTO shift_replica (
        shift_id, store_id, business_date, opened_by_employee_id, closed_by_employee_id,
        opened_at, closed_at, opening_cash, expected_cash, counted_cash, variance, variance_note
    ) VALUES (
        :shift_id, :store_id, :business_date, :opened_by_employee_id, :closed_by_employee_id,
        :opened_at, :closed_at, :opening_cash, :expected_cash, :counted_cash, :variance,
        :variance_note
    )
    """
)


async def apply_shift_closed(
    session: AsyncSession, env: EventEnvelope, p: ShiftClosedPayload, _rules: ReturnRules
) -> None:
    await session.execute(_INSERT_SHIFT, {"store_id": env.store_id, **p.model_dump()})


# ═══════════════════════════ Bảng định tuyến ═══════════════════════════

#: `SaleReturned` cố ý CHƯA có handler: payload ở docs/12 §3.2 thiếu các cột bắt buộc của
#: `sale_replica` (`business_date`, `employee_id`, `total`...). Cửa hàng chưa phát sự kiện này
#: (trả hàng là việc tuần 2, docs/progress). Service trả `retryable=True` cho loại không có
#: handler, nên nếu cửa hàng lên phiên bản trước trung tâm, sự kiện CHỜ chứ không mất.
HANDLERS: dict[str, Handler] = {
    "SaleCompleted": apply_sale_completed,
    "PointsEarned": apply_points,
    "PointsReturned": apply_points,
    "PointsAdjusted": apply_points,
    "CustomerCreated": apply_customer_created,
    "CustomerUpdated": apply_customer_updated,
    "ShiftClosed": apply_shift_closed,
}


def handler_for(event_type: str) -> Handler | None:
    return HANDLERS.get(event_type)


__all__ = [
    "HANDLERS",
    "PermanentEventError",
    "RetryableEventError",
    "handler_for",
    "lifetime_contribution",
]
