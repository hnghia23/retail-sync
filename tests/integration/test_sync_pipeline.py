"""Đồng bộ cửa hàng → trung tâm, đầu-cuối trên Postgres THẬT — docs/16 §2.

Đường đi thật, không mock ở giữa:
    `place_sale` (edge) → outbox → `run_once` (sync worker) → HTTP → `POST /events`
    (Central API thật qua ASGI) → `processed_event` + replica + ledger + balance

Mỗi test gắn với một kịch bản nghiệm thu hoặc một tình huống vận hành cụ thể — tên kịch bản
nằm trong docstring để truy ngược về docs/01 §5 và docs/business/02.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from edge.loyalty.api import CustomerSnapshot, build_loyalty_service
from edge.pos.adapters.postgres import (
    PostgresProductCatalog,
    PostgresSaleRepository,
    PostgresShifts,
)
from edge.pos.application.place_sale import (
    PlaceSaleCommand,
    RequestedLine,
    RequestedPayment,
    place_sale,
)
from edge.sync.worker import run_once
from shared.config import PricingRules, ReturnRules, SyncSettings
from shared.db import transaction
from shared.events import PointsPayload
from shared.outbox import OutboxPublisher
from shared.types import utcnow
from tests.integration.conftest import Central

STORE_ID = "store-001"
OTHER_STORE_ID = "store-002"
CUSTOMER_ID = uuid.UUID("01935abc-0000-7000-8000-000000000001")

SYNC = SyncSettings(_env_file=None)


# ═══════════════════════════ Fixture ═══════════════════════════


async def _seed_edge(db: Any) -> uuid.UUID:
    shift_id = uuid.uuid4()
    await db.execute(
        """INSERT INTO shift (shift_id, store_id, business_date, opened_by_employee_id,
                              opening_cash)
           VALUES ($1, $2, CURRENT_DATE, 'emp-007', 500000)""",
        shift_id,
        STORE_ID,
    )
    await db.executemany(
        """INSERT INTO product_cache (product_id, sku, barcode, name, unit_price, is_sellable)
           VALUES ($1, $2, $3, $4, $5, true)""",
        [
            ("SKU-001", "S1", "8930001", "Nuoc mam", 150_000),
            ("SKU-002", "S2", "8930002", "Gao", 100_000),
        ],
    )
    await db.execute(
        """INSERT INTO customer_local (customer_id, phone_hash, tier)
           VALUES ($1, 'hash-1', 'GOLD')""",
        CUSTOMER_ID,
    )
    return shift_id


async def _sell(edge: Any, shift_id: uuid.UUID) -> Any:
    """Một đơn 400.000đ cho khách GOLD → 380.000đ, 38 điểm. Đi đúng đường route chạy nó."""
    command = PlaceSaleCommand(
        employee_id="emp-007",
        shift_id=shift_id,
        lines=[RequestedLine("SKU-001", 2), RequestedLine("SKU-002", 1)],
        payments=[RequestedPayment("CASH", 380_000)],
        customer=CustomerSnapshot(
            customer_id=CUSTOMER_ID, tier="GOLD", balance=0, lifetime_earned=5_000
        ),
        occurred_at=utcnow(),
    )
    async with transaction(edge) as session:
        return await place_sale(
            command,
            catalog=PostgresProductCatalog(session),
            shifts=PostgresShifts(session),
            sales=PostgresSaleRepository(session),
            events=OutboxPublisher(session, store_id=STORE_ID),
            loyalty=await build_loyalty_service(session, store_id=STORE_ID, at=command.occurred_at),
            rules=PricingRules(_env_file=None),
        )


async def _outbox_envelopes(edge_db: Any) -> list[dict[str, Any]]:
    rows = await edge_db.fetch("SELECT payload FROM outbox ORDER BY id")
    return [json.loads(r["payload"]) for r in rows]


def _unreachable() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("mất mạng (giả lập)", request=request)

    return httpx.MockTransport(handler)


# ═══════════════════════════ Đường thành công ═══════════════════════════


async def test_sale_reaches_central_end_to_end(central: Central, edge: Any, edge_db: Any) -> None:
    """AT-01 — đơn thường có khách, đi hết đường tới trung tâm.

    Đây cũng là test hợp đồng: payload do `place_sale` THẬT sinh ra phải qua được kiểm tra
    của trung tâm. v1 chết đúng ở những đường nối kiểu này (docs/09).
    """
    receipt = await _sell(edge, await _seed_edge(edge_db))

    report = await run_once(edge, central.client(), settings=SYNC)
    assert report.transport_error is None
    assert (report.sent, report.retry_scheduled, report.dead_lettered) == (2, 0, 0)

    sale = await central.db.fetchrow("SELECT * FROM sale_replica")
    assert sale["sale_id"] == receipt.sale_id
    assert sale["store_id"] == STORE_ID
    assert sale["total"] == 380_000
    assert await central.db.fetchval("SELECT count(*) FROM sale_line_replica") == 2
    assert await central.db.fetchval("SELECT sum(amount) FROM sale_payment_replica") == 380_000

    # docs/12 §3.3 — cùng một `event_id` ở ledger cửa hàng, outbox, và ledger trung tâm.
    local_id = await edge_db.fetchval("SELECT event_id FROM point_ledger_local")
    assert await central.db.fetchval("SELECT event_id FROM point_ledger") == local_id

    balance = await central.db.fetchrow(
        "SELECT * FROM point_balance WHERE customer_id = $1", CUSTOMER_ID
    )
    assert (balance["balance"], balance["lifetime_earned"], balance["tier"]) == (38, 38, "BRONZE")

    status = await central.db.fetchrow(
        "SELECT * FROM store_sync_status WHERE store_id = $1", STORE_ID
    )
    assert status["status"] == "OK"


async def test_sent_events_mark_local_ledger_synced(
    central: Central, edge: Any, edge_db: Any
) -> None:
    """Trigger `0003_edge_sync`: `outbox.sent_at` kéo theo `point_ledger_local.synced_at`.

    Không có nó, số dư hiển thị ở quầy (= số dư trung tâm + dòng cục bộ chưa gửi, docs/03 §4.3)
    sẽ cộng điểm hai lần sau khi cửa hàng kéo số dư mới từ trung tâm về.
    """
    await _sell(edge, await _seed_edge(edge_db))
    assert await edge_db.fetchval("SELECT synced_at FROM point_ledger_local") is None

    await run_once(edge, central.client(), settings=SYNC)

    assert await edge_db.fetchval("SELECT count(*) FROM outbox WHERE sent_at IS NULL") == 0
    assert await edge_db.fetchval("SELECT synced_at FROM point_ledger_local") is not None


# ═══════════════════ AT-03 / CH-7 — gửi lặp ═══════════════════


async def test_resending_same_batch_counts_points_once(
    central: Central, edge: Any, edge_db: Any
) -> None:
    """AT-03 (gửi lặp 5 lần) và CH-7 (gửi lại cả lô 10 lần) — điểm chỉ cộng MỘT lần.

    Lần gửi lặp vẫn được trả về trong `accepted`: với cửa hàng, "trung tâm đã có" là tất cả.
    """
    await _sell(edge, await _seed_edge(edge_db))
    envelopes = await _outbox_envelopes(edge_db)
    client = central.client()

    for _ in range(10):
        result = await client.push(envelopes)
        assert len(result.accepted) == 2
        assert result.rejected == []

    assert await central.db.fetchval("SELECT count(*) FROM point_ledger") == 1
    assert await central.db.fetchval("SELECT count(*) FROM sale_replica") == 1
    assert await central.db.fetchval("SELECT balance FROM point_balance") == 38


# ═══════════════════ AT-02 — mất mạng không được làm hại dữ liệu ═══════════════════


async def test_central_unreachable_never_consumes_attempts(
    central: Central, edge: Any, edge_db: Any
) -> None:
    """AT-02 — lỗi quan trọng nhất có thể viết ở sync worker.

    Nếu mất mạng cũng tính vào `attempts`, một cửa hàng offline vài phút sẽ tự đẩy dữ liệu
    đúng của mình vào dead-letter. Ở đây: 25 vòng mất mạng (> `max_attempts` = 10) rồi nối
    lại — không sự kiện nào bị đụng tới, rồi tất cả lên đủ.
    """
    await _sell(edge, await _seed_edge(edge_db))
    offline = central.client(transport=_unreachable())

    for _ in range(25):
        report = await run_once(edge, offline, settings=SYNC)
        assert report.transport_error is not None
        assert report.sent == report.retry_scheduled == report.dead_lettered == 0

    row = await edge_db.fetchrow(
        "SELECT max(attempts) AS attempts, count(dead_lettered_at) AS dead FROM outbox"
    )
    assert (row["attempts"], row["dead"]) == (0, 0)

    report = await run_once(edge, central.client(), settings=SYNC)
    assert report.sent == 2
    assert await central.db.fetchval("SELECT balance FROM point_balance") == 38


async def test_backpressure_is_a_transport_error_with_retry_after(
    central: Central, edge: Any, edge_db: Any
) -> None:
    """docs/08 §3.2 — trung tâm quá tải trả `503 + Retry-After`, worker lùi đúng số giây đó."""
    await _sell(edge, await _seed_edge(edge_db))
    central.app.state.ingest_gate = asyncio.Semaphore(0)  # mọi chỗ đã bị chiếm

    report = await run_once(edge, central.client(), settings=SYNC)

    assert report.transport_error == "http 503"
    assert report.retry_after == 5
    assert await edge_db.fetchval("SELECT max(attempts) FROM outbox") == 0


# ═══════════════════ Từng sự kiện độc lập ═══════════════════


async def test_points_wait_for_missing_customer_then_land(
    central: Central, edge: Any, edge_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Khóa ngoại vỡ = sự kiện phụ thuộc chưa tới → thử lại được, KHÔNG dead-letter.

    Và cùng lô đó, `SaleCompleted` vẫn qua — một sự kiện lỗi không được kéo cả lô theo
    (docs/13 §2).
    """
    # Cố định backoff: jitter toàn phần có thể ra vài mili-giây ở lượt đầu (có chủ đích — chống
    # đồng pha), khiến khẳng định "chưa tới hạn" trượt ~1–3% số lần chạy. Đã tái hiện chắc
    # chắn bằng cách ép backoff = 0 trước khi sửa.
    monkeypatch.setattr("edge.sync.worker.next_backoff", lambda *_a, **_k: 60.0)
    await central.db.execute("DELETE FROM customer")
    await _sell(edge, await _seed_edge(edge_db))

    report = await run_once(edge, central.client(), settings=SYNC)
    assert (report.sent, report.retry_scheduled, report.dead_lettered) == (1, 1, 0)
    assert await central.db.fetchval("SELECT count(*) FROM sale_replica") == 1

    pending = await edge_db.fetchrow(
        "SELECT attempts, last_error, next_attempt_at > now() AS later FROM outbox "
        "WHERE event_type = 'PointsEarned'"
    )
    assert pending["attempts"] == 1
    assert pending["last_error"] == "foreign_key_violation"
    assert pending["later"], "phải lùi theo next_attempt_at, không lấy lại ngay vòng sau"

    # Chưa tới hạn → vòng tiếp theo không đụng tới nó.
    assert (await run_once(edge, central.client(), settings=SYNC)).claimed == 0

    # Khách tới trung tâm; tua đồng hồ qua hạn thử lại.
    await central.db.execute(
        "INSERT INTO customer (customer_id, phone_hash) VALUES ($1, 'hash-1')", CUSTOMER_ID
    )
    await edge_db.execute("UPDATE outbox SET next_attempt_at = now() - interval '1 second'")

    report = await run_once(edge, central.client(), settings=SYNC)
    assert report.sent == 1
    assert await central.db.fetchval("SELECT balance FROM point_balance") == 38


async def test_poison_event_is_dead_lettered_immediately(
    central: Central, edge: Any, edge_db: Any
) -> None:
    """Sự kiện hỏng về cấu trúc: dead-letter ngay lượt đầu, không đốt 10 lượt vô ích.

    Và nó không chặn sự kiện hợp lệ phía sau.
    """
    await _sell(edge, await _seed_edge(edge_db))
    bad_id = uuid.uuid4()
    bad = {
        "event_id": str(bad_id),
        "event_type": "SaleCompleted",
        "schema_version": 99,  # trung tâm chưa biết version này
        "store_id": STORE_ID,
        "occurred_at": utcnow().isoformat(),
        "payload": {},
    }
    await edge_db.execute(
        "INSERT INTO outbox (event_id, event_type, payload) VALUES ($1, 'SaleCompleted', $2)",
        bad_id,
        json.dumps(bad),
    )

    report = await run_once(edge, central.client(), settings=SYNC)
    assert (report.sent, report.dead_lettered) == (2, 1)

    dead = await edge_db.fetchrow(
        "SELECT attempts, last_error FROM outbox WHERE dead_lettered_at IS NOT NULL"
    )
    assert dead["attempts"] == 1
    assert dead["last_error"].startswith("unsupported_schema_version")
    # Trung tâm cũng giữ bản sao để người vận hành xem — docs/12 §4.
    assert await central.db.fetchval("SELECT count(*) FROM dead_letter_event") == 1
    # Dead-letter không bao giờ bị lấy lại.
    assert (await run_once(edge, central.client(), settings=SYNC)).claimed == 0


async def test_retryable_rejection_dead_letters_after_max_attempts(
    central: Central, edge: Any, edge_db: Any
) -> None:
    """Sự kiện kẹt mãi (khách không bao giờ tới) không được chặn hàng đợi vĩnh viễn — ADR-003."""
    await central.db.execute("DELETE FROM customer")
    await _sell(edge, await _seed_edge(edge_db))
    settings = SyncSettings(_env_file=None, max_attempts_before_dead_letter=3)

    for _ in range(3):
        await run_once(edge, central.client(), settings=settings)
        await edge_db.execute("UPDATE outbox SET next_attempt_at = now() - interval '1 second'")

    row = await edge_db.fetchrow(
        "SELECT attempts, dead_lettered_at FROM outbox WHERE event_type = 'PointsEarned'"
    )
    assert row["attempts"] == 3
    assert row["dead_lettered_at"] is not None


# ═══════════════════ Xác thực cửa hàng ═══════════════════


async def test_store_cannot_send_events_as_another_store(
    central: Central, edge: Any, edge_db: Any
) -> None:
    """Khóa cửa hàng B không ghi được sự kiện mang danh cửa hàng A.

    Cùng lớp lỗi với việc `POST /sales` từng tin `employee_id` client gửi lên (vá ở tuần 1).
    """
    await _sell(edge, await _seed_edge(edge_db))
    envelopes = await _outbox_envelopes(edge_db)

    result = await central.client(OTHER_STORE_ID).push(envelopes)

    assert result.accepted == []
    assert {r.reason.split(":")[0] for r in result.rejected} == {"store_mismatch"}
    assert all(not r.retryable for r in result.rejected)
    assert await central.db.fetchval("SELECT count(*) FROM sale_replica") == 0


async def test_missing_or_wrong_key_is_401(central: Central) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=central.app), base_url="http://c"
    ) as raw:
        assert (await raw.post("/api/v1/events", json=[])).status_code == 401
        wrong = await raw.post(
            "/api/v1/events", json=[], headers={"Authorization": "Bearer khong-dung"}
        )
        assert wrong.status_code == 401


async def test_oversized_batch_is_413(central: Central) -> None:
    from central.settings import get_settings

    too_many = [{"event_id": str(uuid.uuid4())}] * (get_settings().ingest_max_batch + 1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=central.app), base_url="http://c"
    ) as raw:
        resp = await raw.post(
            "/api/v1/events",
            json=too_many,
            headers={"Authorization": f"Bearer {central.keys[STORE_ID]}"},
        )
    assert resp.status_code == 413


# ═══════════════════ AT-04 — hai cửa hàng, cùng khách, cùng lúc ═══════════════════


def _points_envelope(store_id: str, delta: int) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "PointsEarned",
        "schema_version": 1,
        "store_id": store_id,
        "occurred_at": utcnow().isoformat(),
        "payload": PointsPayload(customer_id=CUSTOMER_ID, delta=delta, reason="EARN").model_dump(
            mode="json"
        ),
    }


async def test_concurrent_points_from_two_stores_sum_exactly(central: Central) -> None:
    """AT-04 phía trung tâm — lý do tồn tại của ledger (ADR-002).

    Hai cửa hàng đẩy điểm cho CÙNG một khách trong các request song song. Với mô hình
    "đọc số dư, cộng, ghi lại" v1 sẽ mất cập nhật; với ledger + upsert cộng dồn thì tổng luôn
    đúng tuyệt đối.
    """
    batches = [
        [_points_envelope(store, 7) for _ in range(20)] for store in (STORE_ID, OTHER_STORE_ID) * 5
    ]
    clients = {s: central.client(s) for s in (STORE_ID, OTHER_STORE_ID)}
    # 10 request song song > giới hạn backpressure mặc định (8). Test này về tính đúng của
    # phép cộng đồng thời, không phải về backpressure — nới giới hạn để không nhận 503.
    central.app.state.ingest_gate = asyncio.Semaphore(len(batches))

    results = await asyncio.gather(
        *(clients[batch[0]["store_id"]].push(batch) for batch in batches)
    )

    assert all(len(r.accepted) == 20 and not r.rejected for r in results)
    expected = 10 * 20 * 7
    assert await central.db.fetchval("SELECT count(*) FROM point_ledger") == 200
    balance = await central.db.fetchrow("SELECT * FROM point_balance")
    assert balance["balance"] == expected
    # INV-4: snapshot bằng đúng tổng ledger.
    assert await central.db.fetchval("SELECT sum(delta) FROM point_ledger") == expected
    # 1.400 điểm tích lũy → SILVER theo quy tắc TRUNG TÂM (ngưỡng 1.000).
    assert balance["tier"] == "SILVER"


# ═══════════════════ C03 — trùng SĐT giữa hai cửa hàng offline ═══════════════════


async def test_duplicate_phone_is_flagged_not_rejected(central: Central) -> None:
    """Hai cửa hàng offline cùng đăng ký một SĐT → hai `customer_id`. Cả hai phải tồn tại ở
    trung tâm (nếu không, điểm của khách thứ hai mất vì vỡ khóa ngoại), và cặp đó được đưa
    vào danh sách chờ gộp THỦ CÔNG.
    """
    second = uuid.UUID("01935abc-0000-7000-8000-000000000002")
    event_id = uuid.uuid4()
    envelope = {
        "event_id": str(event_id),
        "event_type": "CustomerCreated",
        "schema_version": 1,
        "store_id": OTHER_STORE_ID,
        "occurred_at": utcnow().isoformat(),
        "payload": {
            "customer_id": str(second),
            "phone_hash": "hash-1",  # trùng khách đã có
            "created_locally_at_store": OTHER_STORE_ID,
        },
    }

    result = await central.client(OTHER_STORE_ID).push([envelope])

    assert result.accepted == [event_id]
    assert (
        await central.db.fetchval("SELECT count(*) FROM customer WHERE phone_hash = 'hash-1'") == 2
    )
    ids = await central.db.fetchval(
        "SELECT customer_ids FROM customer_duplicate_candidate WHERE phone_hash = 'hash-1'"
    )
    assert set(ids) == {CUSTOMER_ID, second}


async def test_central_api_transactions_are_bounded(central: Central) -> None:
    """docs/17 §4 bẫy 1: trích xuất dừng ở transaction đang mở cũ nhất, nên một transaction
    treo của Central API làm trích xuất ĐỨNG. `transaction_timeout` cắt trần thời gian đứng."""
    from sqlalchemy import text

    async with central.app.state.session_factory() as session:
        value = (await session.execute(text("SHOW transaction_timeout"))).scalar_one()
    assert value == "1min"


async def test_registered_customer_points_and_closed_shift_reach_central(
    central: Central, edge: Any, edge_db: Any
) -> None:
    """Đường dữ liệu đầy đủ của giai đoạn A: đăng ký khách → bán → đóng ca → đồng bộ.

    Trước khi có `register_customer`, điểm chưa bao giờ đi qua đường đồng bộ thật — test
    khác chèn sẵn khách ở CẢ HAI đầu. Ở đây khách chỉ tồn tại ở cửa hàng, và phải tới trung
    tâm qua `CustomerCreated`, trước điểm của chính nó, trong CÙNG một lô.
    """
    from datetime import timedelta

    from edge.pos.application.shifts import (
        CloseShiftCommand,
        OpenShiftCommand,
        close_shift,
        open_shift,
    )

    await _seed_edge(edge_db)
    await edge_db.execute("UPDATE shift SET status = 'CLOSED', closed_at = now()")
    registered_at = utcnow() - timedelta(hours=3)  # đăng ký lúc offline, 3 giờ trước

    async with transaction(edge) as session:
        shift = await open_shift(
            OpenShiftCommand(
                store_id=STORE_ID,
                employee_id="emp-007",
                business_date=(utcnow() + timedelta(hours=7)).date(),
                opening_cash=0,
                opened_at=utcnow(),
                store_utc_offset=timedelta(hours=7),
            ),
            shifts=PostgresShifts(session),
        )
        loyalty = await build_loyalty_service(
            session, store_id=STORE_ID, at=registered_at, phone_hash_key="test-key"
        )
        customer = await loyalty.register_customer(phone="0933111222", occurred_at=registered_at)

    command = PlaceSaleCommand(
        employee_id="emp-007",
        shift_id=shift.shift_id,
        lines=[RequestedLine("SKU-001", 2)],
        payments=[RequestedPayment("CASH", 300_000)],
        customer=CustomerSnapshot(
            customer_id=customer.customer_id, tier="BRONZE", balance=0, lifetime_earned=0
        ),
        occurred_at=utcnow(),
    )
    async with transaction(edge) as session:
        await place_sale(
            command,
            catalog=PostgresProductCatalog(session),
            shifts=PostgresShifts(session),
            sales=PostgresSaleRepository(session),
            events=OutboxPublisher(session, store_id=STORE_ID),
            loyalty=await build_loyalty_service(session, store_id=STORE_ID, at=command.occurred_at),
            rules=PricingRules(_env_file=None),
        )
    async with transaction(edge) as session:
        await close_shift(
            CloseShiftCommand(shift.shift_id, STORE_ID, "emp-007", 300_000, None, utcnow()),
            shifts=PostgresShifts(session),
            events=OutboxPublisher(session, store_id=STORE_ID),
        )

    report = await run_once(edge, central.client(), settings=SYNC)
    # CustomerCreated + SaleCompleted + PointsEarned + ShiftClosed — không sự kiện nào phải chờ.
    assert (report.sent, report.retry_scheduled, report.dead_lettered) == (4, 0, 0)

    row = await central.db.fetchrow(
        "SELECT joined_at, phone_hash FROM customer WHERE customer_id = $1", customer.customer_id
    )
    # Ngày gia nhập = lúc đăng ký TẠI CỬA HÀNG, không phải lúc sự kiện tới trung tâm.
    assert abs((row["joined_at"] - registered_at).total_seconds()) < 1
    balance = await central.db.fetchval(
        "SELECT balance FROM point_balance WHERE customer_id = $1", customer.customer_id
    )
    assert balance == 30

    closed = await central.db.fetchrow(
        "SELECT expected_cash, variance, business_date FROM shift_replica WHERE shift_id = $1",
        shift.shift_id,
    )
    assert (closed["expected_cash"], closed["variance"]) == (300_000, 0)
    assert closed["business_date"] == shift.business_date


async def test_reconciliation_finds_no_drift_after_real_ingest(
    central: Central, edge: Any, edge_db: Any
) -> None:
    """AT-10 trên dữ liệu đi qua ĐÚNG đường ghi production: job đối soát và handler ingest
    phải cùng một định nghĩa `lifetime_earned`/hạng — lệch ở đây là lệch định nghĩa."""
    from central.ops.reconcile import reconcile_points

    shift_id = await _seed_edge(edge_db)
    for _ in range(3):
        await _sell(edge, shift_id)
    report = await run_once(edge, central.client(), settings=SYNC)
    assert report.sent == 6

    result = await reconcile_points(
        central.app.state.session_factory, rules=ReturnRules(_env_file=None), safety_lag_seconds=0
    )
    assert result.checked_customers == 1
    assert result.drifts == []


# ═══════════════ CH-6 — rate limit theo cửa hàng, heartbeat (giai đoạn B) ═══════════════


async def test_store_rate_limit_is_429_with_retry_after_and_spares_other_stores(
    central: Central,
) -> None:
    """docs/08 §3.2 — MỘT cửa hàng xả tồn đọng không được chiếm hết lượt: nó nhận `429` +
    `Retry-After`, cửa hàng khác vẫn gửi được. Với worker đó là lỗi đường truyền (không đốt lượt
    thử), và nó ngủ đúng số giây trung tâm bảo."""
    from central.ingest.ratelimit import StoreRateLimiter
    from edge.sync.client import CentralUnavailableError

    central.app.state.store_limiter = StoreRateLimiter(rate=0.5, burst=2)
    busy, other = central.client(STORE_ID), central.client(OTHER_STORE_ID)
    await busy.push([])
    await busy.push([])
    with pytest.raises(CentralUnavailableError) as refused:
        await busy.push([])
    assert refused.value.status_code == 429
    assert refused.value.retry_after == 2  # (1 - 0 token) / 0.5 req/s, làm tròn lên
    await other.push([])  # bucket riêng: cửa hàng khác không bị vạ lây


async def test_empty_batch_is_a_heartbeat_that_marks_the_store_seen(
    central: Central, edge: Any, edge_db: Any
) -> None:
    """Lô rỗng = cửa hàng còn sống (`store_last_seen_age_seconds`), không cần có đơn nào."""
    await _sell(edge, await _seed_edge(edge_db))
    await run_once(edge, central.client(), settings=SYNC)  # có dòng store_sync_status
    # Lô cuối là một lô xả tồn đọng 2 giờ: trễ 7200 s. Heartbeat = outbox đã trống → trễ về 0.
    await central.db.execute(
        "UPDATE store_sync_status SET updated_at = now() - interval '3 hours',"
        " lag_seconds = 7200, status = 'LAGGING' WHERE store_id = $1",
        STORE_ID,
    )
    await central.client().push([])
    age, lag, status = await central.db.fetchrow(
        "SELECT EXTRACT(EPOCH FROM now() - updated_at), lag_seconds, status"
        " FROM store_sync_status WHERE store_id = $1",
        STORE_ID,
    )
    assert age < 60 and lag == 0 and status == "OK"


async def test_heartbeat_of_a_store_that_never_sold_makes_it_visible(central: Central) -> None:
    """Cửa hàng chưa từng gửi sự kiện nào vẫn phải có dòng sau heartbeat đầu tiên — không thì
    `rs-store-silent` không bao giờ thấy nó khi worker chết (workflow `proof`, 2026-09-28)."""
    await central.db.execute("DELETE FROM store_sync_status WHERE store_id = $1", STORE_ID)
    await central.client().push([])
    row = await central.db.fetchrow(
        "SELECT last_event_at, lag_seconds, status, EXTRACT(EPOCH FROM now() - updated_at) AS age"
        " FROM store_sync_status WHERE store_id = $1",
        STORE_ID,
    )
    assert row is not None, "heartbeat của cửa hàng mới không để lại dấu vết nào"
    assert row["last_event_at"] is None and row["lag_seconds"] == 0 and row["status"] == "OK"
    assert row["age"] < 60


async def test_idle_worker_detects_lost_central_through_heartbeat(edge: Any) -> None:
    """Không có gì để gửi mà trung tâm mất: không có heartbeat thì worker không bao giờ biết, và
    `sync_consecutive_failures` (circuit breaker, docs/08 §5) đứng ở 0 đúng lúc phải kêu."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from edge.sync.telemetry import SyncMetrics
    from edge.sync.worker import run_forever

    reader = InMemoryMetricReader()
    metrics = SyncMetrics(STORE_ID, meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    settings = SyncSettings(
        _env_file=None, heartbeat_seconds=0, poll_interval_seconds=0.05, backoff_max_seconds=0.05
    )
    offline = HttpCentralClientFactory.unreachable()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_forever(edge, offline, settings=settings, stop=stop, metrics=metrics)
    )
    await asyncio.sleep(0.6)
    stop.set()
    await task

    data = reader.get_metrics_data()
    assert data is not None
    values = {
        m.name: [p.value for p in m.data.data_points]
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }
    assert max(values["sync_consecutive_failures"]) > 0
    assert sum(values["sync_push_failures"]) > 0


async def test_heartbeat_keeps_an_idle_store_seen(central: Central, edge: Any) -> None:
    """Worker rảnh, trung tâm sống: heartbeat đi qua, không lỗi, không lô nào được "gửi"."""
    from edge.sync.worker import heartbeat

    report = await heartbeat(central.client())
    assert report.heartbeat and report.transport_error is None and report.claimed == 0


class HttpCentralClientFactory:
    @staticmethod
    def unreachable() -> Any:
        from edge.sync.client import HttpCentralClient

        return HttpCentralClient(
            base_url="http://central-down", api_key="k", timeout_seconds=1, transport=_unreachable()
        )
