"""Central API. Chạy:  uv run uvicorn central.main:app --port 8000

Hiện chỉ có `/health`. `POST /events` và các route tra cứu được mount khi có thân thật —
hợp đồng ở docs/13-api-contracts.md §2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy import text

from central.settings import get_otel_settings, get_settings
from shared.db import make_engine, make_session_factory
from shared.tracing import instrument_clients, instrument_fastapi, setup_tracing


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = make_engine(settings.database_url, echo=settings.debug)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    setup_tracing("central-api", settings=get_otel_settings())

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

    if get_otel_settings().enabled:
        instrument_fastapi(app)
        instrument_clients()

    return app


app = create_app()
