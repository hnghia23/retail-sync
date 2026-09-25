"""Nhận một lô sự kiện từ cửa hàng — docs/13 §2, docs/14 §4.

## Hợp đồng quan trọng nhất: từng sự kiện ĐỘC LẬP

Một sự kiện lỗi không được làm rớt cả lô. Nếu cả lô cùng thất bại, worker sẽ gửi lại
cả 200 sự kiện dù chỉ 1 cái hỏng — vô hại nhờ idempotency, nhưng lãng phí và làm chậm hội tụ,
và một sự kiện độc sẽ chặn cả hàng đợi phía sau nó.

Cơ chế: một transaction cho cả lô, mỗi sự kiện một SAVEPOINT. Sự kiện lỗi → hoàn tác đúng
savepoint của nó, phần còn lại vẫn commit. Kể cả việc chốt `processed_event` cũng nằm TRONG
savepoint: sự kiện áp không thành thì cũng không bị đánh dấu "đã xử lý".

## Phân loại lỗi — quyết định số phận sự kiện ở cửa hàng

| Lỗi                                              | `retryable` | Ghi `dead_letter_event` |
|--------------------------------------------------|:-----------:|:-----------------------:|
| envelope/payload sai, `schema_version` lạ, sai cửa hàng | False | có |
| khóa ngoại (sự kiện phụ thuộc chưa tới)          | True        | không |
| unique/check/not-null, dữ liệu sai kiểu (SQLSTATE 22/23) | False | có |
| chưa có handler cho loại sự kiện                  | True        | không |
| lỗi DB khác, bug                                  | True        | không |

Nghiêng về `True` khi không chắc: gửi lại thừa thì idempotency lo, vứt nhầm thì mất dữ liệu.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from central.ingest.handlers import PermanentEventError, RetryableEventError, handler_for
from central.ingest.idempotency import claim_event
from shared.events import EventEnvelope, IngestResult, RejectedEvent
from shared.tracing import link_from_trace

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from shared.config import ReturnRules

log = logging.getLogger(__name__)

#: docs/12 §4 — version chưa hỗ trợ thì dead-letter và cảnh báo, KHÔNG cố đoán ý nghĩa.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

#: FR-C10 / docs/08 §5 — trễ quá ngưỡng này thì cửa hàng bị coi là `LAGGING`.
LAGGING_AFTER_SECONDS = 900

_MAX_REASON = 500

_DEAD_LETTER = text(
    """
    INSERT INTO dead_letter_event (event_id, store_id, event_type, schema_version, payload, error)
    VALUES (:event_id, :store_id, :event_type, :schema_version, CAST(:payload AS jsonb), :error)
    ON CONFLICT (event_id) DO UPDATE SET
        error = EXCLUDED.error, payload = EXCLUDED.payload, received_at = now()
    """
)

_SYNC_STATUS = text(
    """
    INSERT INTO store_sync_status (store_id, last_event_at, lag_seconds, status, updated_at)
    VALUES (:store_id, CAST(:last_event_at AS timestamptz),
            GREATEST(0, EXTRACT(EPOCH FROM now() - CAST(:last_event_at AS timestamptz)))::integer,
            CASE WHEN now() - CAST(:last_event_at AS timestamptz)
                      > make_interval(secs => CAST(:lagging_after AS double precision))
                 THEN 'LAGGING' ELSE 'OK' END,
            now())
    ON CONFLICT (store_id) DO UPDATE SET
        last_event_at = GREATEST(store_sync_status.last_event_at, EXCLUDED.last_event_at),
        lag_seconds   = EXCLUDED.lag_seconds,
        status        = EXCLUDED.status,
        updated_at    = now()
    """
)


#: Heartbeat (lô rỗng): worker chỉ gửi nó khi outbox ĐÃ TRỐNG — cửa hàng không còn gì chưa tới
#: trung tâm, nên trễ đồng bộ lúc này đúng là 0. Không đặt lại thì "trễ của lô cuối" (một lô xả
#: tồn đọng nhiều giờ) nằm nguyên đó mãi dù cửa hàng đã bắt kịp, và cảnh báo `rs-store-lag` kêu
#: vĩnh viễn (thấy ở compose 2026-09-25). Cửa hàng chưa từng gửi sự kiện nào thì chưa có dòng.
_SEEN = text(
    "UPDATE store_sync_status SET updated_at = now(), lag_seconds = 0, status = 'OK'"
    " WHERE store_id = :store_id"
)


async def mark_seen(session: AsyncSession, *, store_id: str) -> None:
    await session.execute(_SEEN, {"store_id": store_id})


@dataclass(frozen=True, slots=True)
class _Outcome:
    event_id: uuid.UUID | None
    accepted: bool = False
    retryable: bool = True
    reason: str = ""
    occurred_at: datetime | None = None
    duplicate: bool = False


@dataclass(slots=True)
class IngestStats:
    """Đếm kết cục của một lô cho metric `ingest_events_total{outcome}` (docs/08 §5).

    Tách khỏi `IngestResult` vì đó là hợp đồng HTTP với cửa hàng (docs/13 §2): với cửa hàng,
    "trùng" và "mới" đều là `accepted` (AT-03), phân biệt hai thứ chỉ có nghĩa với người vận
    hành — `event_duplicate_rate` xác nhận idempotency đang làm việc.
    """

    accepted: int = 0
    duplicate: int = 0
    rejected_retryable: int = 0
    rejected_permanent: int = 0
    unreadable: int = 0

    def outcomes(self) -> dict[str, int]:
        return {
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "rejected_retryable": self.rejected_retryable,
            "rejected_permanent": self.rejected_permanent,
            "unreadable": self.unreadable,
        }


async def ingest_batch(
    session: AsyncSession,
    raw_events: Sequence[Mapping[str, Any]],
    *,
    store_id: str,
    rules: ReturnRules,
    stats: IngestStats | None = None,
) -> IngestResult:
    """Áp một lô trong transaction ĐANG MỞ của `session`. Gọi viên commit.

    `raw_events` là dict thô chứ không phải `EventEnvelope` đã ép kiểu: nếu để FastAPI ép cả
    mảng, MỘT envelope hỏng làm cả request `422` — đúng thứ hợp đồng "từng sự kiện độc lập"
    cấm. Ép từng cái ở đây thì cái hỏng chỉ nằm trong `rejected`.

    `stats` (tùy chọn) nhận số đếm theo kết cục. Người gọi chỉ nên ghi metric SAU khi commit:
    lô rollback sẽ được gửi lại nguyên vẹn, đếm trước commit là đếm hai lần.
    """
    stats = stats if stats is not None else IngestStats()
    result = IngestResult()
    newest: datetime | None = None

    for raw in raw_events:
        outcome = await _ingest_one(session, raw, store_id=store_id, rules=rules)
        if outcome.event_id is None:
            # Không đọc được `event_id` thì không có gì để báo lại. Phía cửa hàng coi sự kiện
            # vắng mặt trong cả hai danh sách là lỗi thử lại được — nên nó vẫn tiến tới
            # dead-letter sau N lần, không kẹt vĩnh viễn.
            stats.unreadable += 1
            continue
        if outcome.accepted:
            if outcome.duplicate:
                stats.duplicate += 1
            else:
                stats.accepted += 1
            result.accepted.append(outcome.event_id)
            if outcome.occurred_at and (newest is None or outcome.occurred_at > newest):
                newest = outcome.occurred_at
            continue

        result.rejected.append(
            RejectedEvent(
                event_id=outcome.event_id, reason=outcome.reason, retryable=outcome.retryable
            )
        )
        if outcome.retryable:
            stats.rejected_retryable += 1
        else:
            stats.rejected_permanent += 1
            await _dead_letter(session, raw, outcome)

    if newest is not None:
        await session.execute(
            _SYNC_STATUS,
            {
                "store_id": store_id,
                "last_event_at": newest,
                "lagging_after": LAGGING_AFTER_SECONDS,
            },
        )
    return result


async def _ingest_one(
    session: AsyncSession, raw: Mapping[str, Any], *, store_id: str, rules: ReturnRules
) -> _Outcome:
    event_id = _peek_event_id(raw)
    if event_id is None:
        log.warning("ingest: bỏ qua sự kiện không có event_id hợp lệ (store=%s)", store_id)
        return _Outcome(event_id=None)

    try:
        env = EventEnvelope.model_validate(raw)
    except ValidationError as exc:
        return _reject(event_id, f"invalid_envelope: {_describe(exc)}", retryable=False)

    if env.store_id != store_id:
        # Khóa của cửa hàng A không được ghi sự kiện mang danh cửa hàng B.
        return _reject(event_id, f"store_mismatch: key={store_id}", retryable=False)
    if env.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        return _reject(
            event_id, f"unsupported_schema_version: {env.schema_version}", retryable=False
        )

    try:
        payload = env.parsed_payload()
    except ValidationError as exc:
        return _reject(event_id, f"invalid_payload: {_describe(exc)}", retryable=False)

    handler = handler_for(env.event_type)
    if handler is None:
        return _reject(event_id, f"no_handler: {env.event_type}", retryable=True)

    from opentelemetry import trace

    carrier = {k: v for k, v in env.trace.model_dump().items() if isinstance(v, str) and v}
    tracer = trace.get_tracer("central.ingest")
    # Ràng buộc #7: span LINK tới trace gốc ở cửa hàng, không phải quan hệ cha-con — sự kiện
    # có thể tới sau hàng giờ, một span cha dài hàng giờ làm hỏng mọi thống kê thời lượng.
    with tracer.start_as_current_span(
        "ingest.event",
        links=link_from_trace(carrier),
        attributes={"event.type": env.event_type, "event.id": str(event_id), "store.id": store_id},
    ):
        try:
            async with session.begin_nested():
                if not await claim_event(session, env.event_id):
                    # Đã xử lý ở lần gửi trước. Vẫn là `accepted`: với cửa hàng, "trung tâm đã
                    # có" là điều duy nhất quan trọng — đây chính là AT-03.
                    return _Outcome(event_id=event_id, accepted=True, duplicate=True)
                await handler(session, env, payload, rules)
        except PermanentEventError as exc:
            return _reject(event_id, f"invalid_payload: {exc}", retryable=False)
        except RetryableEventError as exc:
            return _reject(event_id, f"not_ready: {exc}", retryable=True)
        except DBAPIError as exc:
            retryable, reason = _classify_db_error(exc)
            return _reject(event_id, reason, retryable=retryable)
        except Exception:
            # Bug của trung tâm, không phải lỗi của sự kiện → giữ lại để gửi lại sau khi sửa.
            log.exception("ingest: lỗi không mong đợi, event_id=%s", event_id)
            return _reject(event_id, "internal_error", retryable=True)

    return _Outcome(event_id=event_id, accepted=True, occurred_at=env.occurred_at)


def _classify_db_error(exc: DBAPIError) -> tuple[bool, str]:
    """SQLSTATE → (retryable, lý do). SQLAlchemy gắn `sqlstate` của asyncpg vào `exc.orig`."""
    sqlstate: str = getattr(exc.orig, "sqlstate", None) or ""
    if sqlstate == "23503":
        # Khóa ngoại: thường là khách chưa tới (`CustomerCreated` đang chờ) — gửi lại sẽ qua.
        return True, "foreign_key_violation"
    if sqlstate.startswith("23"):
        # Unique/check/not-null. Gồm cả "no partition found for row" (23514) — sự kiện có
        # `occurred_at` ngoài mọi partition: sai đồng hồ cửa hàng hoặc dữ liệu quá cũ.
        return False, f"integrity_violation: {sqlstate}"
    if sqlstate.startswith("22"):
        return False, f"data_exception: {sqlstate}"
    return True, f"db_error: {sqlstate or type(exc.orig).__name__}"


def _peek_event_id(raw: Mapping[str, Any]) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw.get("event_id")))
    except (ValueError, TypeError, AttributeError):
        return None


def _describe(exc: ValidationError) -> str:
    """Tóm tắt lỗi Pydantic KHÔNG kèm giá trị đầu vào.

    `str(ValidationError)` in cả `input_value` — với payload khách hàng, đó có thể là thứ ta
    không muốn nằm trong log hay trong `dead_letter_event.error` (ràng buộc #10).
    """
    parts = [
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    ]
    return "; ".join(parts)[:_MAX_REASON]


def _reject(event_id: uuid.UUID, reason: str, *, retryable: bool) -> _Outcome:
    log.info("ingest: từ chối event_id=%s retryable=%s reason=%s", event_id, retryable, reason)
    return _Outcome(event_id=event_id, retryable=retryable, reason=reason[:_MAX_REASON])


async def _dead_letter(session: AsyncSession, raw: Mapping[str, Any], outcome: _Outcome) -> None:
    version = raw.get("schema_version")
    await session.execute(
        _DEAD_LETTER,
        {
            "event_id": outcome.event_id,
            "store_id": str(raw.get("store_id")) if raw.get("store_id") is not None else None,
            "event_type": str(raw.get("event_type")) if raw.get("event_type") else None,
            "schema_version": version if isinstance(version, int) else None,
            "payload": json.dumps(raw, default=str),
            "error": outcome.reason,
        },
    )
