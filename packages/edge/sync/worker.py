"""Vòng lặp đẩy outbox — docs/03 §4.2, docs/14 §4, ADR-003.

Bốn thứ PHẢI có ngay từ bản đầu (thêm sau rất đau):
  1. `FOR UPDATE SKIP LOCKED` — scale seam sẵn có: chạy N worker song song, không sửa gì.
  2. Backoff lũy thừa + jitter — 10 cửa hàng nối lại cùng lúc không được dội trung tâm (CH-6).
  3. Dead-letter sau N lần — sự kiện độc không được chặn hàng đợi vĩnh viễn.
  4. Span link từ `trace` trong payload — nối trace gốc với lần gửi hàng giờ sau (ràng buộc #7).

## Hai loại thất bại, hai cách lùi — KHÔNG được trộn lẫn

| Thất bại | Ví dụ | Lùi bằng | Tăng `attempts`? |
|---|---|---|---|
| Của đường truyền | mất mạng, timeout, 401, 503 | ngủ cả vòng lặp | **Không** |
| Của một sự kiện | trung tâm trả trong `rejected` | `next_attempt_at` của RIÊNG nó | Có |

Trộn hai loại là lỗi nguy hiểm nhất có thể viết ở đây: nếu mất mạng cũng tính vào lượt thử,
một cửa hàng offline 20 vòng quét (vài phút) sẽ tự đẩy dữ liệu đúng của mình vào dead-letter —
chính xác điều AT-02 kiểm. `attempts` chỉ đếm số lần TRUNG TÂM đã xét và từ chối sự kiện.

Worker là tầng vận chuyển: không hiểu nội dung sự kiện, không import `pos`/`loyalty`
(`import-linter`, hợp đồng `sync-is-transport-only`). Việc đánh dấu `point_ledger_local.synced_at`
do trigger DB làm khi `outbox.sent_at` được đặt (migration `0003_edge_sync`).

Chạy:  uv run python -m edge.sync.worker
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from edge.sync.backoff import next_backoff
from edge.sync.client import CentralUnavailableError
from shared.db import transaction
from shared.tracing import link_from_trace

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from edge.sync.client import CentralClient
    from edge.sync.telemetry import SyncMetrics
    from shared.config import SyncSettings
    from shared.events import IngestResult

log = logging.getLogger(__name__)

# Lấy lô sự kiện đến hạn gửi. Điều kiện `sent_at`/`dead_lettered_at` PHẢI khớp partial index
# `outbox_unsent_idx`, nếu không planner không dùng được nó. SKIP LOCKED để nhiều worker không
# giẫm chân nhau.
CLAIM_BATCH = text(
    """
    SELECT id, event_id, event_type, payload, attempts
    FROM outbox
    WHERE sent_at IS NULL
      AND dead_lettered_at IS NULL
      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
    ORDER BY id
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
    """
)

MARK_SENT = text(
    "UPDATE outbox SET sent_at = now(), last_error = NULL WHERE event_id = ANY(:event_ids)"
)

SCHEDULE_RETRY = text(
    """
    UPDATE outbox
    SET attempts = attempts + 1,
        last_error = :error,
        next_attempt_at = now() + make_interval(secs => CAST(:delay AS double precision))
    WHERE event_id = :event_id
    """
)

DEAD_LETTER = text(
    """
    UPDATE outbox
    SET attempts = attempts + 1, last_error = :error, dead_lettered_at = now()
    WHERE event_id = :event_id
    """
)

# Dọn outbox (ràng buộc #9) — theo lô, theo thứ tự `id` (khóa chính): dòng cũ nhất nằm ở đầu
# chỉ mục nên không cần index riêng trên `sent_at`. Chỉ dòng ĐÃ GỬI: dòng chưa gửi (cửa hàng offline
# nhiều ngày) và dòng dead-letter không bao giờ bị đụng.
PRUNE_SENT = text(
    """
    DELETE FROM outbox WHERE id IN (
        SELECT id FROM outbox
        WHERE sent_at < now() - make_interval(secs => CAST(:retention AS double precision))
        ORDER BY id
        LIMIT :batch
    )
    """
)

#: SDK OpenTelemetry mặc định giữ tối đa 128 link mỗi span; cắt trước thay vì để nó âm thầm bỏ.
_MAX_LINKS = 128


@dataclass(frozen=True, slots=True)
class BatchReport:
    """Kết quả một vòng. `transport_error` khác None nghĩa là không sự kiện nào bị đụng tới."""

    claimed: int = 0
    sent: int = 0
    retry_scheduled: int = 0
    dead_lettered: int = 0
    transport_error: str | None = None
    retry_after: float | None = None


async def claim_batch(session: AsyncSession, *, batch_size: int) -> list[dict[str, Any]]:
    """Giữ một lô sự kiện đến hạn trong transaction hiện tại."""
    rows = (await session.execute(CLAIM_BATCH, {"batch_size": batch_size})).mappings().all()
    return [dict(r) for r in rows]


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    client: CentralClient,
    *,
    settings: SyncSettings,
) -> BatchReport:
    """Một vòng: giữ lô → gửi → ghi kết quả. Toàn bộ trong MỘT transaction.

    Khóa hàng được giữ suốt lúc chờ HTTP (tối đa `request_timeout_seconds`). Đó là cái giá của
    việc không cần cột "đang gửi": crash giữa chừng thì transaction rollback, khóa tự nhả, lô
    quay về hàng đợi nguyên vẹn — không có trạng thái treo nào phải dọn.
    """
    async with transaction(session_factory) as session:
        batch = await claim_batch(session, batch_size=settings.batch_size)
        if not batch:
            return BatchReport()

        envelopes = [_envelope(row) for row in batch]
        try:
            result = await _push(client, envelopes)
        except CentralUnavailableError as exc:
            # Không ghi gì: transaction commit rỗng, khóa nhả, `attempts` giữ nguyên.
            return BatchReport(
                claimed=len(batch), transport_error=str(exc), retry_after=exc.retry_after
            )
        return await _apply(session, batch, result, settings=settings)


async def _push(client: CentralClient, envelopes: list[dict[str, Any]]) -> IngestResult:
    from opentelemetry import trace

    links = [link for env in envelopes for link in link_from_trace(env.get("trace"))]
    tracer = trace.get_tracer("edge.sync")
    with tracer.start_as_current_span(
        "sync.push_batch",
        links=links[:_MAX_LINKS],
        attributes={"sync.batch_size": len(envelopes)},
    ):
        return await client.push(envelopes)


async def _apply(
    session: AsyncSession,
    batch: list[dict[str, Any]],
    result: IngestResult,
    *,
    settings: SyncSettings,
) -> BatchReport:
    rows = {_uuid(row["event_id"]): row for row in batch}

    # Chỉ tin ID thuộc lô vừa gửi — trung tâm trả ID lạ thì bỏ qua, không đánh dấu bừa.
    accepted = {eid for eid in result.accepted if eid in rows}
    if accepted:
        await session.execute(MARK_SENT, {"event_ids": list(accepted)})

    retried = dead = 0
    handled = set(accepted)
    for rejection in result.rejected:
        if rejection.event_id not in rows or rejection.event_id in handled:
            continue
        handled.add(rejection.event_id)
        if await _record_failure(
            session,
            rows[rejection.event_id],
            reason=rejection.reason,
            retryable=rejection.retryable,
            settings=settings,
        ):
            dead += 1
        else:
            retried += 1

    # Vắng mặt trong cả hai danh sách → trung tâm không đọc được sự kiện này. Tính là một lần
    # từ chối thử-lại-được: nó vẫn tiến tới dead-letter sau N lần, không kẹt đầu hàng mãi mãi.
    for eid, row in rows.items():
        if eid in handled:
            continue
        if await _record_failure(
            session, row, reason="missing_in_response", retryable=True, settings=settings
        ):
            dead += 1
        else:
            retried += 1

    return BatchReport(
        claimed=len(batch), sent=len(accepted), retry_scheduled=retried, dead_lettered=dead
    )


async def _record_failure(
    session: AsyncSession,
    row: dict[str, Any],
    *,
    reason: str,
    retryable: bool,
    settings: SyncSettings,
) -> bool:
    """Trả `True` nếu sự kiện vừa vào dead-letter."""
    attempts = int(row["attempts"]) + 1
    if not retryable or attempts >= settings.max_attempts_before_dead_letter:
        await session.execute(DEAD_LETTER, {"event_id": row["event_id"], "error": reason})
        log.error(
            "sync: dead-letter event_id=%s type=%s attempts=%d reason=%s",
            row["event_id"],
            row["event_type"],
            attempts,
            reason,
        )
        return True

    delay = next_backoff(attempts, max_seconds=settings.backoff_max_seconds)
    await session.execute(
        SCHEDULE_RETRY, {"event_id": row["event_id"], "error": reason, "delay": delay}
    )
    return False


async def prune_sent(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retention_days: float,
    batch: int = 1000,
) -> int:
    """Xóa sự kiện đã gửi quá `retention_days`, mỗi lô một transaction ngắn. Trả số dòng đã xóa.

    Lô nhỏ, transaction riêng: không giữ khóa lâu trên bảng mà đường bán hàng đang ghi vào.
    """
    total = 0
    while True:
        async with transaction(session_factory) as session:
            result = await session.execute(
                PRUNE_SENT, {"retention": retention_days * 86400, "batch": batch}
            )
        deleted = int(getattr(result, "rowcount", 0) or 0)
        total += deleted
        if deleted < batch:
            return total


async def run_forever(
    session_factory: async_sessionmaker[AsyncSession],
    client: CentralClient,
    *,
    settings: SyncSettings,
    stop: asyncio.Event,
    metrics: SyncMetrics | None = None,
) -> None:
    """Vòng lặp chính. Không bao giờ tự thoát vì lỗi — chỉ dừng khi `stop` được đặt.

    Cửa hàng không có IT tại chỗ (docs/business/01 §4): worker chết là outbox dồn im lặng cho
    tới khi có người để ý. Nên mọi lỗi — kể cả DB cửa hàng tạm chết — chỉ làm nó lùi lại.
    """
    failures = 0
    loop = asyncio.get_running_loop()
    next_prune = loop.time()  # dọn một lượt ngay khi khởi động, rồi theo chu kỳ
    while not stop.is_set():
        if loop.time() >= next_prune:
            next_prune = loop.time() + settings.prune_interval_seconds
            try:
                pruned = await prune_sent(
                    session_factory, retention_days=settings.outbox_retention_days
                )
                if metrics is not None:
                    metrics.record_pruned(pruned)
                if pruned:
                    log.info("sync: dọn %d sự kiện đã gửi quá hạn giữ", pruned)
            except Exception:
                log.exception("sync: dọn outbox lỗi, thử lại chu kỳ sau")

        try:
            report = await run_once(session_factory, client, settings=settings)
        except Exception:
            log.exception("sync: lỗi cục bộ, sẽ thử lại")
            report = BatchReport(transport_error="local_error")

        if report.transport_error:
            failures += 1
            delay = (
                report.retry_after
                if report.retry_after is not None
                else next_backoff(failures, max_seconds=settings.backoff_max_seconds)
            )
            log.warning(
                "sync: không gửi được (lần %d): %s — thử lại sau %.1fs",
                failures,
                report.transport_error,
                delay,
            )
        else:
            if failures:
                log.info("sync: đã nối lại trung tâm sau %d lần lỗi", failures)
            failures = 0
            if report.claimed:
                log.info(
                    "sync: sent=%d retry=%d dead=%d",
                    report.sent,
                    report.retry_scheduled,
                    report.dead_lettered,
                )
            # Lô đầy = còn tồn đọng → gửi tiếp ngay, không chờ. Đây là cách xả nhanh 3 ngày
            # dữ liệu dồn lại sau khi mạng phục hồi (B02 I05).
            delay = 0.0 if report.claimed >= settings.batch_size else settings.poll_interval_seconds

        if metrics is not None:
            metrics.record(report, consecutive_failures=failures)

        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass


def _envelope(row: dict[str, Any]) -> dict[str, Any]:
    """`payload` của outbox đã là cả envelope (xem `shared.outbox.enqueue_event`)."""
    payload = row["payload"]
    return json.loads(payload) if isinstance(payload, str) else dict(payload)


def _uuid(value: object) -> uuid.UUID:
    """asyncpg trả kiểu UUID riêng của nó; ép về `uuid.UUID` để so sánh/băm chắc chắn đúng."""
    return value if type(value) is uuid.UUID else uuid.UUID(str(value))


async def _main() -> None:  # pragma: no cover — điểm vào tiến trình
    import signal

    from edge.settings import get_otel_settings, get_settings, get_sync_settings
    from edge.sync.client import HttpCentralClient
    from edge.sync.telemetry import SyncMetrics
    from shared.db import make_engine, make_session_factory
    from shared.metrics import setup_metrics
    from shared.tracing import instrument_clients, setup_tracing

    settings = get_settings()
    sync = get_sync_settings()
    if not settings.central_api_key:
        raise SystemExit(
            "CENTRAL_API_KEY chưa đặt. Cấp khóa ở trung tâm: "
            f"python -m central.ops.provision_store --store-id {settings.store_id}"
        )

    otel = get_otel_settings()
    setup_tracing(f"sync-worker-{settings.store_id}", settings=otel)
    setup_metrics(f"sync-worker-{settings.store_id}", settings=otel)
    if otel.enabled:
        instrument_clients()

    engine = make_engine(settings.database_url)
    client = HttpCentralClient(
        base_url=settings.central_api_url,
        api_key=settings.central_api_key,
        timeout_seconds=sync.request_timeout_seconds,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows: không có add_signal_handler; Ctrl+C vẫn dừng qua KeyboardInterrupt

    log.info("sync: bắt đầu, store=%s central=%s", settings.store_id, settings.central_api_url)
    try:
        await run_forever(
            make_session_factory(engine),
            client,
            settings=sync,
            stop=stop,
            metrics=SyncMetrics(settings.store_id),
        )
    finally:
        await client.aclose()
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    from edge.settings import get_settings
    from shared.logs import setup_logging

    setup_logging(f"sync-worker-{get_settings().store_id}")
    asyncio.run(_main())
