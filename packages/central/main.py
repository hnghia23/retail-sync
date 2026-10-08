"""Central API. Chạy:  uv run uvicorn central.main:app --port 8000

Có `/health` và `POST /api/v1/events` (ingest). Các route tra cứu (`lookup`) và báo cáo
được mount khi có thân thật — hợp đồng ở docs/13-api-contracts.md §2.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy import text

from central.ingest.ratelimit import StoreRateLimiter
from central.ingest.router import router as ingest_router
from central.settings import get_otel_settings, get_settings, worker_budget
from shared.db import make_engine, make_session_factory
from shared.logs import setup_logging
from shared.metrics import get_meter, register_resource_gauges, setup_metrics
from shared.tracing import instrument_clients, instrument_fastapi, setup_tracing


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Mỗi tiến trình uvicorn chạy lifespan riêng: semaphore, rate limiter, pool đều THEO TIẾN
    # TRÌNH, nên mỗi tiến trình chỉ lấy phần của nó trong ngân sách tổng (ADR-011).
    budget = worker_budget(settings)
    engine = make_engine(
        settings.database_url,
        echo=settings.debug,
        server_settings={"transaction_timeout": f"{settings.db_transaction_timeout_seconds}s"},
        pool_size=budget.pool_size,
        max_overflow=budget.max_overflow,
    )
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    # Tạo TRONG lifespan, không ở mức module: semaphore gắn với event loop đang chạy.
    app.state.ingest_gate = asyncio.Semaphore(budget.ingest_concurrency)
    app.state.store_limiter = StoreRateLimiter(
        rate=budget.store_rate_per_second, burst=budget.store_burst
    )
    if get_otel_settings().enabled:
        register_resource_gauges(
            get_meter("central.resources"),
            engine=engine,
            attrs={"service": "central-api"},
            pool_capacity=budget.pool_size + budget.max_overflow,
        )
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    setup_logging("central-api")
    setup_tracing("central-api", settings=get_otel_settings())
    # Trước `instrument_fastapi()` — xem `shared.metrics.setup_metrics`.
    setup_metrics("central-api", settings=get_otel_settings())

    app = FastAPI(
        title="retail-sync Central API",
        version="0.2.0",
        description="Hợp nhất sự kiện + master data + báo cáo chuỗi. docs/13-api-contracts.md",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )

    @app.get("/health", tags=["health"])
    async def health(request: Request) -> dict[str, Any]:
        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    app.include_router(ingest_router)

    if get_otel_settings().enabled:
        instrument_fastapi(app)
        instrument_clients()

    return app


app = create_app()
