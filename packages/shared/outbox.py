"""Ghi outbox — ADR-003 + ràng buộc #7.

Một hàm duy nhất, và nó PHẢI được gọi trong CÙNG transaction với việc ghi nghiệp vụ.
Đó là toàn bộ ý nghĩa của Transactional Outbox: sự kiện và dữ liệu cùng commit hoặc
cùng rollback — không có khe hở để mất sự kiện.

Sai lầm cần tránh: gọi hàm này sau `commit()`, hoặc trong một session khác.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import text

from shared.events import SCHEMA_VERSION, EventType
from shared.tracing import inject_trace
from shared.types import new_event_id, utcnow

if TYPE_CHECKING:
    from pydantic import BaseModel
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["OutboxPublisher", "enqueue_event"]

_INSERT = text(
    """
    INSERT INTO outbox (event_id, event_type, payload)
    VALUES (:event_id, :event_type, CAST(:payload AS jsonb))
    """
)


async def enqueue_event(
    session: AsyncSession,
    *,
    event_type: EventType,
    store_id: str,
    payload: BaseModel,
    occurred_at: datetime | None = None,
    event_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Nhét một sự kiện vào outbox trong transaction hiện tại.

    `event_id` truyền vào khi ID đã tồn tại ở nơi khác — cụ thể là sự kiện điểm:
    `event_id` PHẢI bằng `point_ledger.event_id` (docs/12 §3.3), nếu sinh ID mới thì
    dedupe ở trung tâm sẽ không nhận ra bản lặp.
    """
    import json

    eid = event_id or new_event_id()
    envelope = {
        "event_id": str(eid),
        "event_type": event_type,
        "schema_version": SCHEMA_VERSION,
        "store_id": store_id,
        "occurred_at": (occurred_at or utcnow()).isoformat(),
        "payload": payload.model_dump(mode="json"),
        # Ràng buộc #7 — traceparent đi cùng payload, không đi cùng HTTP header
        "trace": inject_trace(),
    }
    await session.execute(
        _INSERT,
        {"event_id": eid, "event_type": event_type, "payload": json.dumps(envelope)},
    )
    return eid


class OutboxPublisher:
    """Lớp mỏng quanh `enqueue_event`, gắn sẵn session và `store_id`.

    Ngoại lệ duy nhất của quy tắc "không dùng chung transaction nghiệp vụ" (ADR-004 quy
    tắc 3): `pos` và `loyalty` cùng ghi vào `outbox` trong một transaction. Đó là điều
    khiến "lưu đơn + tích điểm" đúng đắn miễn phí, không cần saga.
    """

    def __init__(self, session: AsyncSession, *, store_id: str) -> None:
        self._session = session
        self._store_id = store_id

    async def publish(
        self,
        *,
        event_type: EventType,
        payload: BaseModel,
        occurred_at: datetime,
        event_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        return await enqueue_event(
            self._session,
            event_type=event_type,
            store_id=self._store_id,
            payload=payload,
            occurred_at=occurred_at,
            event_id=event_id,
        )
