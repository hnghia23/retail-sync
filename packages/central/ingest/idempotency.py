"""Chốt chặn idempotency — docs/12 §5.

Mẫu duy nhất được dùng, không có biến thể:

    INSERT INTO processed_event (event_id, received_at) VALUES (:event_id, now())
    ON CONFLICT (event_id) DO NOTHING;

Chỉ xử lý payload khi INSERT thực sự chèn được dòng (rowcount = 1). Gửi lại 100 lần vẫn
chỉ ghi một lần — đây là điều AT-03 kiểm.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_CLAIM = text(
    """
    INSERT INTO processed_event (event_id, received_at)
    VALUES (:event_id, now())
    ON CONFLICT (event_id) DO NOTHING
    """
)


async def claim_event(session: AsyncSession, event_id: uuid.UUID) -> bool:
    """`True` = sự kiện mới, hãy xử lý. `False` = đã xử lý rồi, bỏ qua im lặng.

    Phải chạy trong CÙNG transaction với việc ghi dữ liệu nghiệp vụ. Nếu tách ra, một
    crash giữa hai transaction sẽ đánh dấu "đã xử lý" một sự kiện chưa ghi gì → mất dữ
    liệu vĩnh viễn, và là loại mất dữ liệu không ai phát hiện ra.
    """
    result = await session.execute(_CLAIM, {"event_id": event_id})
    # `AsyncSession.execute()` khai kiểu trả `Result[Any]`, thiếu `.rowcount` — nhưng với
    # một câu lệnh INSERT/UPDATE/DELETE thì object thật lúc runtime luôn là `CursorResult`.
    # Đây là khoảng trống trong stub của SQLAlchemy 2, không phải bug ở đây.
    return cast(CursorResult[Any], result).rowcount == 1
