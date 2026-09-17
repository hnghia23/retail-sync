"""Health check — docs/13 §1 "Health & sync".

Điểm mấu chốt: **Redis chết vẫn trả 200**, chỉ gắn cờ `degraded`. Nếu trả 503 thì
orchestrator sẽ restart/loại container trong khi cửa hàng vẫn bán hàng được bình thường
(AT-08). Chỉ Postgres cửa hàng chết mới là lỗi thật (docs/03 §5).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from edge.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, Any]:
    checks: dict[str, str] = {}

    try:
        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "down"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    # Redis suy giảm được — không hạ status code.
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(get_settings().redis_url)
        await client.ping()
        await client.aclose()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "degraded"

    return {
        "status": "ok" if checks["postgres"] == "ok" else "unhealthy",
        "store_id": get_settings().store_id,
        "checks": checks,
    }


@router.get("/health/outbox")
async def outbox_health(request: Request) -> dict[str, Any]:
    """Metric backpressure — docs/08 §5.

    `oldest_unsent_age_seconds` là tín hiệu cảnh báo quan trọng nhất của hệ thống đồng bộ:
    độ sâu outbox có thể nhỏ mà vẫn tắc, nếu một sự kiện đầu hàng bị kẹt.
    """
    sql = text(
        """
        SELECT count(*) AS depth,
               COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) AS oldest_age
        FROM outbox WHERE sent_at IS NULL
        """
    )
    async with request.app.state.session_factory() as session:
        row = (await session.execute(sql)).one()
    return {"outbox_depth": int(row.depth), "oldest_unsent_age_seconds": float(row.oldest_age)}
