"""Edge API — điểm vào duy nhất của tiến trình tại cửa hàng.

Chạy:  uv run uvicorn edge.main:app --port 8000

Đang có: `/health`, `GET /api/v1/products`, `POST /api/v1/sales`, và UI POS ở `/ui/*`
(HTMX + Alpine, docs/06-roadmap.md tuần 1 ngày 5). Route API còn lại (`/returns`,
`/shifts`, `/reports`, `/customers`) được mount khi chúng có thân thật — hợp đồng đã chốt
ở docs/13-api-contracts.md, không lặp lại thành stub trong code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from edge.health import router as health_router
from edge.pos.adapters.router import router as pos_router
from edge.settings import get_otel_settings, get_settings
from edge.web.auth import RequiresLoginError, requires_login_handler
from edge.web.router import router as web_router
from shared.db import make_engine, make_session_factory
from shared.tracing import instrument_clients, instrument_fastapi, setup_tracing

_WEB_STATIC_DIR = Path(__file__).parent / "web" / "static"


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
    settings = get_settings()

    # OTel gắn TỪ ĐẦU — roadmap ngày 5 ghi rõ: thêm sau đắt hơn nhiều (ADR-009).
    setup_tracing(f"edge-api-{settings.store_id}", settings=get_otel_settings())

    app = FastAPI(
        title=f"retail-sync Edge API ({settings.store_id})",
        version="0.2.0",
        description="POS + Loyalty + báo cáo vận hành tại cửa hàng. docs/13-api-contracts.md",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(pos_router, prefix="/api/v1")
    app.include_router(web_router)
    app.mount("/ui/static", StaticFiles(directory=str(_WEB_STATIC_DIR)), name="ui-static")
    app.add_exception_handler(RequiresLoginError, requires_login_handler)

    if get_otel_settings().enabled:
        instrument_fastapi(app)
        instrument_clients()

    return app


app = create_app()
