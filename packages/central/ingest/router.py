"""`POST /api/v1/events` — docs/13 §2, module `ingest`.

Bọc `central.ingest.service.ingest_batch`: xác thực cửa hàng, giới hạn kích thước lô,
backpressure, và mở transaction. Mọi logic nghiệp vụ nằm ở service/handlers.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from central.auth import AuthenticatedStore, require_store
from central.ingest.ratelimit import StoreRateLimiter
from central.ingest.service import IngestStats, ingest_batch, mark_seen
from central.ingest.telemetry import record_batch, record_refused
from central.settings import get_return_rules, get_settings
from shared.db import transaction
from shared.events import IngestResult

router = APIRouter(prefix="/api/v1", tags=["ingest"])


@router.post("/events", response_model=IngestResult)
async def post_events(
    request: Request,
    events: Annotated[list[dict[str, Any]], Body(description="Mảng envelope — docs/12 §2")],
    store: Annotated[AuthenticatedStore, Depends(require_store)],
) -> IngestResult:
    """Nhận một lô. Trả `{accepted, rejected}` — KHÔNG bao giờ fail cả lô vì một sự kiện.

    Mã lỗi HTTP chỉ dành cho lỗi của CẢ REQUEST, và worker coi chúng là lỗi đường truyền
    (không trừ lượt thử của từng sự kiện):

      - `401` — khóa sai.
      - `413` — lô quá lớn so với `INGEST_MAX_BATCH`.
      - `429` + `Retry-After` — CỬA HÀNG NÀY gửi quá `INGEST_STORE_RATE_PER_SECOND`.
      - `503` + `Retry-After` — trung tâm đang quá tải (docs/08 §3.2).

    Lô RỖNG là heartbeat của worker rảnh: chỉ ghi nhận "cửa hàng còn sống"
    (`store_sync_status.updated_at` → `store_last_seen_age_seconds`), không mở transaction ingest.
    """
    settings = get_settings()
    if len(events) > settings.ingest_max_batch:
        record_refused("too_large")
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Lô {len(events)} sự kiện vượt giới hạn {settings.ingest_max_batch}",
        )
    limiter: StoreRateLimiter = request.app.state.store_limiter
    if (wait := limiter.acquire(store.store_id)) is not None:
        record_refused("rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Cửa hàng gửi quá nhanh, thử lại sau",
            headers={"Retry-After": limiter.retry_after_header(wait)},
        )
    if not events:
        async with transaction(request.app.state.session_factory) as session:
            await mark_seen(session, store_id=store.store_id)
        return IngestResult()

    gate: asyncio.Semaphore = request.app.state.ingest_gate
    # Kiểm-rồi-lấy không có `await` xen giữa → nguyên tử trong asyncio. Không xếp hàng chờ:
    # một request chờ semaphore vẫn giữ kết nối và bộ nhớ, đúng thứ đang cạn khi quá tải.
    if gate.locked():
        record_refused("overloaded")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Trung tâm đang quá tải, thử lại sau",
            headers={"Retry-After": str(settings.ingest_retry_after_seconds)},
        )

    stats = IngestStats()
    async with gate, transaction(request.app.state.session_factory) as session:
        result = await ingest_batch(
            session, events, store_id=store.store_id, rules=get_return_rules(), stats=stats
        )
    # Sau commit: lô rollback được cửa hàng gửi lại nguyên vẹn, đếm trước là đếm hai lần.
    record_batch(store.store_id, stats)
    return result
