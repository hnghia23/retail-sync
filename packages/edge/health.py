"""Health check — docs/13 §1 "Health & sync".

Điểm mấu chốt: **Redis chết vẫn trả 200**, chỉ gắn cờ `degraded`. Nếu trả 503 thì
orchestrator sẽ restart/loại container trong khi cửa hàng vẫn bán hàng được bình thường
(AT-08). Chỉ Postgres cửa hàng chết mới là lỗi thật (docs/03 §5).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from edge.settings import get_settings

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)

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
    async with request.app.state.session_factory() as session:
        snap = await read_outbox(session)
    return {
        "outbox_depth": snap.depth,
        "oldest_unsent_age_seconds": snap.oldest_unsent_age_seconds,
        "dead_lettered": snap.dead_lettered,
    }


# ═══════════════════════ Outbox → metric (docs/17 §6, chặng S2) ═══════════════════════
#
# Hai câu lệnh, mỗi câu khớp ĐÚNG điều kiện của một partial index (`outbox_unsent_idx`,
# `outbox_dead_letter_idx`), để một lần đo không quét cả bảng outbox — bảng duy nhất tăng mãi
# cho tới khi có job dọn (ràng buộc #9). Gộp thành một câu `WHERE sent_at IS NULL` thì không
# index nào khớp.
#
# Dead-letter PHẢI tách khỏi "đang chờ". Bản đầu của `/health/outbox` đếm mọi dòng
# `sent_at IS NULL`: chỉ một sự kiện vào dead-letter là `oldest_unsent_age_seconds` tăng mãi, và
# cảnh báo quan trọng nhất của hệ thống kêu vĩnh viễn vì một sự kiện không bao giờ được gửi nữa.
_OUTBOX_PENDING = text(
    """
    SELECT count(*) AS depth,
           COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) AS oldest_age
    FROM outbox WHERE sent_at IS NULL AND dead_lettered_at IS NULL
    """
)
_OUTBOX_DEAD = text("SELECT count(*) FROM outbox WHERE dead_lettered_at IS NOT NULL")


@dataclass(frozen=True, slots=True)
class OutboxSnapshot:
    depth: int
    oldest_unsent_age_seconds: float
    dead_lettered: int


async def read_outbox(session: AsyncSession) -> OutboxSnapshot:
    pending = (await session.execute(_OUTBOX_PENDING)).one()
    dead = (await session.execute(_OUTBOX_DEAD)).scalar_one()
    return OutboxSnapshot(int(pending.depth), float(pending.oldest_age), int(dead))


class OutboxGauges:
    """Gauge `outbox_depth`, `outbox_oldest_unsent_age_seconds`, `outbox_dead_lettered`.

    Đo ở EDGE API, không ở sync worker: worker chết đúng là lúc outbox dồn, và nếu chính worker
    báo chỉ số thì nó im lặng đúng lúc cần kêu nhất (cửa hàng không có IT tại chỗ, B01 §4).

    Callback của gauge chạy ở luồng export của SDK, không gọi được DB async — nên một tác vụ nền
    đọc DB theo chu kỳ (`refresh_forever`) còn callback chỉ đọc bản chụp gần nhất. Bản chụp quá
    cũ (DB cửa hàng chết) thì KHÔNG báo gì: series ngừng, thay vì báo mãi con số cũ trông như
    mọi thứ vẫn ổn.
    """

    def __init__(self, store_id: str, *, interval_seconds: float) -> None:
        self.store_id = store_id
        self.interval_seconds = interval_seconds
        self._snap: OutboxSnapshot | None = None
        self._taken_at = 0.0

    def update(self, snap: OutboxSnapshot, *, at: float | None = None) -> None:
        self._snap, self._taken_at = snap, time.monotonic() if at is None else at

    def current(self, *, now: float | None = None) -> OutboxSnapshot | None:
        now = time.monotonic() if now is None else now
        if self._snap is None or now - self._taken_at > 3 * self.interval_seconds:
            return None
        return self._snap

    def register(self, meter: Meter) -> None:
        from opentelemetry.metrics import Observation

        attrs = {"store_id": self.store_id}

        def gauge(field: str) -> Callable[[Any], list[Observation]]:
            def observe(_options: Any) -> list[Observation]:
                snap = self.current()
                return [] if snap is None else [Observation(getattr(snap, field), attrs)]

            return observe

        for name, field, unit, what in (
            ("outbox_depth", "depth", "{event}", "Sự kiện chờ gửi (không tính dead-letter)"),
            (
                "outbox_oldest_unsent_age",
                "oldest_unsent_age_seconds",
                "s",
                "Tuổi sự kiện chờ cũ nhất",
            ),
            ("outbox_dead_lettered", "dead_lettered", "{event}", "Sự kiện đã vào dead-letter"),
        ):
            meter.create_observable_gauge(
                name, callbacks=[gauge(field)], unit=unit, description=what
            )

    async def refresh_forever(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Chạy tới khi bị hủy. Lỗi DB chỉ làm lỡ một lần đo, không bao giờ làm chết Edge API."""
        while True:
            try:
                async with session_factory() as session:
                    self.update(await read_outbox(session))
            except Exception:
                log.warning("outbox metrics: không đọc được outbox", exc_info=True)
            await asyncio.sleep(self.interval_seconds)
