"""Engine + session factory dùng chung.

Chỉ có cấu hình kết nối. Không có model ORM ở đây — mỗi module giữ model của mình
(ADR-004 quy tắc 4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

#: Kích thước pool MẶC ĐỊNH của engine. Central API nhiều tiến trình chia nhỏ nó (ADR-011,
#: `central.settings.worker_budget`); metric `db_pool_used_ratio` chia cho sức chứa THẬT của pool.
POOL_SIZE = 10
MAX_OVERFLOW = 10


def make_engine(
    database_url: str,
    *,
    echo: bool = False,
    server_settings: Mapping[str, str] | None = None,
    pool_size: int = POOL_SIZE,
    max_overflow: int = MAX_OVERFLOW,
) -> AsyncEngine:
    """Engine async cho Postgres.

    `pool_pre_ping`: cửa hàng mất mạng/DB restart là chuyện thường ngày; không có nó thì
    request đầu sau khi nối lại sẽ lỗi vì connection chết trong pool.

    `server_settings`: tham số Postgres gắn vào MỌI kết nối của engine này (gửi trong gói
    khởi động, không tốn thêm round-trip). Đặt theo tiến trình chứ không `ALTER ROLE`, để
    giới hạn của API không vô tình áp lên migration hay job bảo trì dùng chung role.
    """
    connect_args: dict[str, object] = {}
    if server_settings:
        connect_args["server_settings"] = dict(server_settings)
    return create_async_engine(
        database_url,
        echo=echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def transaction(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Một transaction, commit ở cuối, rollback khi có lỗi.

    Luồng chốt đơn dùng đúng context manager này: sale + sale_line + sale_payment +
    point_ledger + outbox nằm trong MỘT transaction (docs/03 §4.1).
    """
    async with factory() as session, session.begin():
        yield session
