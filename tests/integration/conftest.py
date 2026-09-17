"""Fixture cho test tích hợp — Postgres thật qua testcontainers.

Vì sao Postgres THẬT chứ không sqlite/mock: phần lớn tính đúng đắn của thiết kế này nằm
trong CHECK constraint, constraint trigger hoãn, partial index và bảng phân vùng. Mock đi
là mock mất đúng thứ cần kiểm.

Container dùng chung cho cả session (`scope="session"`) nhưng mỗi test có schema sạch
riêng — khởi động Postgres mất ~3s, chạy migration mất ~0.3s.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Ảnh phải khớp infra/compose.yaml: `uuidv7()` là hàm native của PG 18, không có ở PG 17.
POSTGRES_IMAGE = "postgres:18"


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """DSN của một Postgres 18 dùng chung cho cả phiên test."""
    testcontainers = pytest.importorskip("testcontainers.postgres")

    with testcontainers.PostgresContainer(POSTGRES_IMAGE, driver=None) as pg:
        yield (
            f"postgresql://{pg.username}:{pg.password}"
            f"@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
        )


def _run_migrations(alembic_ini: Path, dsn: str, database: str) -> None:
    """Chạy `alembic upgrade head`. **Gọi qua `asyncio.to_thread`, không gọi trực tiếp.**

    `migrations/env.py` tự mở event loop bằng `asyncio.run()` — đó là cách Alembic async
    hoạt động và là cách nó chạy thật ở production. Gọi từ trong một coroutine của pytest
    sẽ nổ "cannot be called from a running event loop", nên fixture đẩy hàm này sang thread
    khác để nó có loop riêng.
    """
    from alembic import command
    from alembic.config import Config

    url = dsn.rsplit("/", 1)[0] + f"/{database}"
    cfg = Config(str(alembic_ini))
    # env.py cố ý đọc DATABASE_URL từ môi trường thay vì từ alembic.ini — secret không bao
    # giờ nằm trong file cấu hình (docs/09-v1-postmortem.md O1).
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url.replace("postgresql://", "postgresql+asyncpg://")
    try:
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


async def _fresh_database(dsn: str, name: str) -> str:
    import asyncpg

    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    return dsn.rsplit("/", 1)[0] + f"/{name}"


@pytest.fixture
async def edge_db(postgres_dsn: str, request: pytest.FixtureRequest) -> AsyncIterator[object]:
    """Kết nối tới một DB cửa hàng sạch, đã chạy migration."""
    import asyncpg

    name = f"edge_{abs(hash(request.node.nodeid)) % 10**8}"
    url = await _fresh_database(postgres_dsn, name)
    await asyncio.to_thread(
        _run_migrations, ROOT / "packages" / "edge" / "alembic.ini", postgres_dsn, name
    )

    conn = await asyncpg.connect(url)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def central_db(postgres_dsn: str, request: pytest.FixtureRequest) -> AsyncIterator[object]:
    """Kết nối tới một DB trung tâm sạch, đã chạy migration."""
    import asyncpg

    name = f"central_{abs(hash(request.node.nodeid)) % 10**8}"
    url = await _fresh_database(postgres_dsn, name)
    await asyncio.to_thread(
        _run_migrations, ROOT / "packages" / "central" / "alembic.ini", postgres_dsn, name
    )

    conn = await asyncpg.connect(url)
    try:
        yield conn
    finally:
        await conn.close()


STORE_ID = "store-001"


@pytest.fixture
async def http_client(
    edge_db: Any, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    """Dựng `edge.main.app` thật, trỏ vào DB test đã migrate, gọi qua HTTP trong tiến trình.

    Dùng chung cho `test_auth_http.py` (JSON API) và `test_web_ui.py` (UI HTML) — cả hai
    đều cần đúng một thứ: FastAPI app thật, nối vào DB test, gọi qua ASGI transport thay
    vì mock. `get_settings()` (và các `get_*` khác) dùng `lru_cache` — phải `cache_clear()`
    trước khi set biến môi trường mới, nếu không app dùng cấu hình của lần import đầu tiên
    trong tiến trình pytest.
    """
    import httpx

    dbname = await edge_db.fetchval("SELECT current_database()")
    base = postgres_dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")

    monkeypatch.setenv("DATABASE_URL", f"{base}/{dbname}")
    monkeypatch.setenv("STORE_ID", STORE_ID)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-http-integration")

    from edge import main as edge_main
    from edge import settings as edge_settings

    for cached in (
        edge_settings.get_settings,
        edge_settings.get_jwt_settings,
        edge_settings.get_pricing_rules,
        edge_settings.get_return_rules,
        edge_settings.get_otel_settings,
    ):
        cached.cache_clear()

    app = edge_main.create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://edge-under-test"
        ) as client:
            yield client

    for cached in (
        edge_settings.get_settings,
        edge_settings.get_jwt_settings,
        edge_settings.get_pricing_rules,
        edge_settings.get_return_rules,
        edge_settings.get_otel_settings,
    ):
        cached.cache_clear()
