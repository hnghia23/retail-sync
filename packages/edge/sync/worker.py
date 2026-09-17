"""Vòng lặp đẩy outbox — docs/03 §4.2.

Bốn thứ PHẢI có ngay từ bản đầu (thêm sau rất đau):
  1. `FOR UPDATE SKIP LOCKED` — scale seam sẵn có: chạy N worker song song, không sửa gì.
  2. Backoff lũy thừa + jitter — 10 cửa hàng nối lại cùng lúc không được dội trung tâm (CH-6).
  3. Dead-letter sau N lần — sự kiện độc không được chặn hàng đợi vĩnh viễn.
  4. Span link từ `trace` trong payload — nối trace gốc với lần gửi hàng giờ sau (ràng buộc #7).

Chạy:  uv run python -m edge.sync.worker
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Lấy lô sự kiện chưa gửi. SKIP LOCKED để nhiều worker không giẫm chân nhau.
CLAIM_BATCH = text(
    """
    SELECT id, event_id, event_type, payload, attempts
    FROM outbox
    WHERE sent_at IS NULL
    ORDER BY id
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
    """
)

MARK_SENT = text("UPDATE outbox SET sent_at = now() WHERE event_id = ANY(:event_ids)")

MARK_FAILED = text(
    """
    UPDATE outbox
    SET attempts = attempts + 1, last_error = :error
    WHERE event_id = ANY(:event_ids)
    """
)


async def claim_batch(session: AsyncSession, *, batch_size: int) -> list[dict[str, Any]]:
    """Giữ một lô sự kiện chưa gửi trong transaction hiện tại."""
    rows = (await session.execute(CLAIM_BATCH, {"batch_size": batch_size})).mappings().all()
    return [dict(r) for r in rows]


def next_backoff(attempts: int, *, max_seconds: float, base: float = 1.0) -> float:
    """Backoff lũy thừa + jitter toàn phần.

    Jitter *toàn phần* (random trong [0, delay]) chứ không phải ±10%: khi N cửa hàng mất
    mạng cùng lúc rồi nối lại cùng lúc, jitter nhỏ vẫn để chúng đồng pha (CH-6).
    """
    import random

    delay = min(base * (2**attempts), max_seconds)
    return random.uniform(0, delay)  # noqa: S311 — jitter, không phải mục đích mật mã


async def run_forever() -> None:  # pragma: no cover — viết ở tuần 2 ngày 8
    """Vòng lặp chính. Chưa triển khai — roadmap tuần 2, ngày 8."""
    raise NotImplementedError("Sync worker — roadmap tuần 2 ngày 8 (ADR-003)")


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    from edge.settings import get_otel_settings, get_settings
    from shared.tracing import setup_tracing

    setup_tracing(f"sync-worker-{get_settings().store_id}", settings=get_otel_settings())
    asyncio.run(run_forever())
