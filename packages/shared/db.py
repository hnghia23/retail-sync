"""Engine + session factory dùng chung.

Chỉ có cấu hình kết nối. Không có model ORM ở đây — mỗi module giữ model của mình
(ADR-004 quy tắc 4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Engine async cho Postgres.

    `pool_pre_ping`: cửa hàng mất mạng/DB restart là chuyện thường ngày; không có nó thì
    request đầu sau khi nối lại sẽ lỗi vì connection chết trong pool.
    """
    return create_async_engine(
        database_url,
        echo=echo,
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
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
