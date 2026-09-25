"""`POST /customers`, `/shifts/open`, `/shifts/{id}/close` qua FastAPI + Postgres thật.

Ba route này là nguồn dữ liệu còn thiếu của luồng (ADR-010 quy tắc 2): không có khách thì
không có điểm thật nào đi qua đồng bộ; không đóng được ca thì `business_date` kẹt mãi ở
ngày mở ca đầu tiên. Đăng nhập bằng nhân viên do CHÍNH `edge.ops.seed` tạo — đúng đường
bộ giả lập sẽ đi.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

STORE_ID = "store-001"
PASSWORD = "mat-khau-gia-lap"
CASHIER = f"{STORE_ID}-cash-01"
MANAGER = f"{STORE_ID}-mgr-01"


def _today_vn() -> date:
    return (datetime.now(UTC) + timedelta(hours=7)).date()


@pytest.fixture
async def seeded(edge_db: Any, postgres_dsn: str) -> None:
    """Nhân viên dựng sẵn + một sản phẩm — bằng lệnh seed thật, không INSERT tay."""
    from edge.ops.seed import seed_employee_cache
    from shared.db import make_engine, make_session_factory, transaction

    dbname = await edge_db.fetchval("SELECT current_database()")
    base = postgres_dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
    engine = make_engine(f"{base}/{dbname}")
    try:
        async with transaction(make_session_factory(engine)) as session:
            await seed_employee_cache(session, store_id=STORE_ID, password=PASSWORD)
    finally:
        await engine.dispose()
    await edge_db.execute(
        """INSERT INTO product_cache (product_id, sku, barcode, name, unit_price, is_sellable)
           VALUES ('SKU-001', 'S1', '8930001', 'Nuoc mam', 150000, true)"""
    )


async def _token(client: Any, employee_id: str) -> dict[str, str]:
    r = await client.post(
        "/api/v1/auth/login", json={"employee_id": employee_id, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _open(client: Any, headers: dict[str, str], opening_cash: int = 500_000) -> str:
    r = await client.post(
        "/api/v1/shifts/open",
        json={"business_date": _today_vn().isoformat(), "opening_cash": opening_cash},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return str(r.json()["shift_id"])


async def _sell(
    client: Any,
    headers: dict[str, str],
    shift_id: str,
    *,
    customer_id: str | None = None,
    payments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body = {
        "employee_id": CASHIER,
        "shift_id": shift_id,
        "customer_id": customer_id,
        "lines": [{"product_id": "SKU-001", "quantity": 2}],
        "payments": payments or [{"method": "CASH", "amount": 300_000}],
    }
    r = await client.post("/api/v1/sales", json=body, headers=headers)
    return {**(r.json() if r.content else {}), "http": r.status_code}


async def _outbox(db: Any) -> list[tuple[str, dict[str, Any]]]:
    rows = await db.fetch("SELECT event_type, payload FROM outbox ORDER BY id")
    return [(r["event_type"], json.loads(r["payload"])) for r in rows]


# ═══════════════════════ POST /customers — FR-L02 ═══════════════════════


async def test_register_customer_writes_hash_only_and_emits_customer_created(
    edge_db: Any, http_client: Any, seeded: None
) -> None:
    headers = await _token(http_client, CASHIER)
    r = await http_client.post("/api/v1/customers", json={"phone": "0901 234 567"}, headers=headers)
    assert r.status_code == 201, r.text
    customer_id = r.json()["customer_id"]

    row = await edge_db.fetchrow(
        "SELECT * FROM customer_local WHERE customer_id = $1", uuid.UUID(customer_id)
    )
    assert row["created_locally"] is True
    assert row["phone_enc"] is None and row["name_enc"] is None

    [(event_type, envelope)] = await _outbox(edge_db)
    assert event_type == "CustomerCreated"
    assert envelope["payload"]["customer_id"] == customer_id
    assert envelope["payload"]["phone_hash"] == row["phone_hash"]
    assert envelope["payload"]["created_locally_at_store"] == STORE_ID

    # Ràng buộc #10: số đọc được không nằm ở đâu cả — cả DB lẫn sự kiện sẽ rời cửa hàng.
    everything = json.dumps(envelope) + str(dict(row))
    assert "0901234567" not in everything and "234 567" not in everything


async def test_same_phone_in_another_spelling_returns_the_same_customer(
    edge_db: Any, http_client: Any, seeded: None
) -> None:
    """Thu ngân gõ lại SĐT khách quen → 200 + khách cũ, KHÔNG phát thêm `CustomerCreated`."""
    headers = await _token(http_client, CASHIER)
    first = await http_client.post(
        "/api/v1/customers", json={"phone": "0901234567"}, headers=headers
    )
    again = await http_client.post(
        "/api/v1/customers", json={"phone": "+84 90 123 4567"}, headers=headers
    )

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == {"customer_id": first.json()["customer_id"], "created": False}
    assert [t for t, _ in await _outbox(edge_db)] == ["CustomerCreated"]


async def test_concurrent_registration_of_one_phone_creates_one_customer(
    edge_db: Any, http_client: Any, seeded: None
) -> None:
    """Hai quầy cùng đăng ký một SĐT cùng lúc → một khách, một sự kiện, không lỗi 500."""
    headers = await _token(http_client, CASHIER)
    results = await asyncio.gather(
        *(
            http_client.post("/api/v1/customers", json={"phone": "0912000111"}, headers=headers)
            for _ in range(8)
        )
    )
    assert sorted(r.status_code for r in results) == [200] * 7 + [201]
    assert len({r.json()["customer_id"] for r in results}) == 1
    assert (
        await edge_db.fetchval("SELECT count(*) FROM outbox WHERE event_type = 'CustomerCreated'")
        == 1
    )


async def test_invalid_phone_is_422_without_echoing_it(http_client: Any, seeded: None) -> None:
    headers = await _token(http_client, CASHIER)
    r = await http_client.post(
        "/api/v1/customers", json={"phone": "12345678901234"}, headers=headers
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_PHONE"
    assert "12345678901234" not in r.text


async def test_customer_created_is_queued_before_the_customers_points(
    edge_db: Any, http_client: Any, seeded: None
) -> None:
    """Outbox gửi theo `id`: `CustomerCreated` phải đi TRƯỚC `PointsEarned` của cùng khách,
    nếu không trung tâm vỡ khóa ngoại và điểm phải chờ thử lại."""
    headers = await _token(http_client, CASHIER)
    shift_id = await _open(http_client, headers)
    customer_id = (
        await http_client.post("/api/v1/customers", json={"phone": "0987654321"}, headers=headers)
    ).json()["customer_id"]

    sale = await _sell(http_client, headers, shift_id, customer_id=customer_id)
    assert sale["http"] == 201 and sale["points_earned"] > 0

    types = [t for t, _ in await _outbox(edge_db)]
    assert types.index("CustomerCreated") < types.index("PointsEarned")


# ═══════════════════════ POST /shifts/open — FR-P11 ═══════════════════════


async def test_open_shift_then_second_open_is_409_with_the_open_shift_id(
    http_client: Any, seeded: None
) -> None:
    headers = await _token(http_client, CASHIER)
    shift_id = await _open(http_client, headers)

    r = await http_client.post(
        "/api/v1/shifts/open",
        json={"business_date": _today_vn().isoformat(), "opening_cash": 0},
        headers=headers,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == {
        "code": "SHIFT_ALREADY_OPEN",
        "message": r.json()["detail"]["message"],
        "shift_id": shift_id,
    }


async def test_business_date_far_from_today_is_422(http_client: Any, seeded: None) -> None:
    headers = await _token(http_client, CASHIER)
    r = await http_client.post(
        "/api/v1/shifts/open",
        json={"business_date": (_today_vn() - timedelta(days=5)).isoformat(), "opening_cash": 0},
        headers=headers,
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_BUSINESS_DATE"


# ═══════════════════════ POST /shifts/{id}/close ═══════════════════════


async def test_close_shift_counts_only_the_cash_part_and_emits_shift_closed(
    edge_db: Any, http_client: Any, seeded: None
) -> None:
    """Đơn tách thanh toán: chỉ phần TIỀN MẶT vào két (FR-P14)."""
    cashier = await _token(http_client, CASHIER)
    manager = await _token(http_client, MANAGER)
    shift_id = await _open(http_client, cashier, opening_cash=500_000)

    assert (await _sell(http_client, cashier, shift_id))["http"] == 201
    split = [{"method": "CASH", "amount": 100_000}, {"method": "CARD", "amount": 200_000}]
    assert (await _sell(http_client, cashier, shift_id, payments=split))["http"] == 201

    r = await http_client.post(
        f"/api/v1/shifts/{shift_id}/close", json={"counted_cash": 890_000}, headers=manager
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["cash_collected"] == 400_000
    assert report["expected_cash"] == 900_000
    assert report["variance"] == -10_000

    closed = [p for t, p in await _outbox(edge_db) if t == "ShiftClosed"]
    assert len(closed) == 1
    assert closed[0]["payload"]["expected_cash"] == 900_000
    assert closed[0]["payload"]["closed_by_employee_id"] == MANAGER

    # Ca đã đóng: không bán thêm được, không đóng lần hai được, và mở được ca mới.
    assert (await _sell(http_client, cashier, shift_id))["http"] == 409
    again = await http_client.post(
        f"/api/v1/shifts/{shift_id}/close", json={"counted_cash": 0}, headers=manager
    )
    assert again.status_code == 409
    assert await _open(http_client, cashier) != shift_id


async def test_cashier_cannot_close_a_shift(http_client: Any, seeded: None) -> None:
    cashier = await _token(http_client, CASHIER)
    shift_id = await _open(http_client, cashier)
    r = await http_client.post(
        f"/api/v1/shifts/{shift_id}/close", json={"counted_cash": 0}, headers=cashier
    )
    assert r.status_code == 403


async def test_close_waits_for_a_sale_that_is_committing(
    edge_db: Any, http_client: Any, seeded: None, postgres_dsn: str
) -> None:
    """Race đóng ca ↔ chốt đơn: đơn đang chốt dở phải được đếm, không được lọt.

    Khe hở nằm GIỮA lúc `place_sale` đọc ca và lúc nó chèn dòng `sale`. Sau khi chèn, khóa
    ngoại `sale → shift` (FOR KEY SHARE) đã tự chặn `FOR UPDATE` của đóng ca; trước đó thì
    chỉ `FOR SHARE` ở câu đọc ca chặn được. Phiên A dừng đúng trong khe hở đó: đã đọc ca
    bằng ĐÚNG câu lệnh của adapter, CHƯA chèn gì. Đóng ca phải đợi; A chèn đơn và commit;
    đóng ca đếm được đơn đó. Bỏ `FOR SHARE` thì đóng ca chạy xong ngay, và đơn của A rơi vào
    một ca đã đóng mà không có trong `expected_cash`.
    """
    import asyncpg

    from edge.pos.adapters.postgres import PostgresShifts

    cashier = await _token(http_client, CASHIER)
    manager = await _token(http_client, MANAGER)
    shift_id = await _open(http_client, cashier, opening_cash=0)

    dbname = await edge_db.fetchval("SELECT current_database()")
    in_flight = await asyncpg.connect(postgres_dsn.rsplit("/", 1)[0] + f"/{dbname}")
    closing: asyncio.Task[Any] | None = None
    try:
        tx = in_flight.transaction()
        await tx.start()
        read_shift = str(PostgresShifts._OPEN_SHIFT).replace(":shift_id", "$1")
        assert await in_flight.fetchrow(read_shift, uuid.UUID(shift_id)) is not None

        closing = asyncio.create_task(
            http_client.post(
                f"/api/v1/shifts/{shift_id}/close", json={"counted_cash": 70_000}, headers=manager
            )
        )
        await asyncio.sleep(0.5)
        waited = not closing.done()

        if waited:
            sale_id = uuid.uuid4()
            await in_flight.execute(
                """INSERT INTO sale (sale_id, store_id, shift_id, business_date, employee_id,
                                     subtotal, total, occurred_at)
                   SELECT $1, $2, shift_id, business_date, $3, 70000, 70000, now()
                   FROM shift WHERE shift_id = $4""",
                sale_id,
                STORE_ID,
                CASHIER,
                uuid.UUID(shift_id),
            )
            await in_flight.execute(
                "INSERT INTO sale_line (sale_id, line_no, product_id, quantity, unit_price,"
                " line_total) VALUES ($1, 1, 'SKU-001', 1, 70000, 70000)",
                sale_id,
            )
            await in_flight.execute(
                "INSERT INTO sale_payment (sale_id, seq, method, amount)"
                " VALUES ($1, 1, 'CASH', 70000)",
                sale_id,
            )
        await tx.commit()
        r = await asyncio.wait_for(closing, timeout=10)
    finally:
        await in_flight.close()

    assert waited, "đóng ca không đợi đơn đang chốt dở — đơn đó sẽ nằm ngoài expected_cash"
    assert r.status_code == 200, r.text
    assert r.json()["cash_collected"] == 70_000
    assert r.json()["variance"] == 0


async def test_quote_total_is_exactly_what_the_sale_charges(
    edge_db: Any, http_client: Any, seeded: None
) -> None:
    """`POST /sales/quote` và `POST /sales` dùng CHUNG một hàm tính tiền: trả đúng số tạm
    tính thì đơn phải qua, kể cả khi có chiết khấu hạng (GOLD 5%, làm tròn theo cấu hình)."""
    headers = await _token(http_client, CASHIER)
    shift_id = await _open(http_client, headers)
    customer_id = (
        await http_client.post("/api/v1/customers", json={"phone": "0977000111"}, headers=headers)
    ).json()["customer_id"]
    await edge_db.execute(
        "UPDATE customer_local SET tier = 'GOLD' WHERE customer_id = $1", uuid.UUID(customer_id)
    )

    lines = [{"product_id": "SKU-001", "quantity": 3}]
    q = await http_client.post(
        "/api/v1/sales/quote", json={"lines": lines, "customer_id": customer_id}, headers=headers
    )
    assert q.status_code == 200, q.text
    quote = q.json()
    assert quote["tier_discount_pct"] == 5
    assert quote["total"] == quote["subtotal"] - quote["discount_tier"] < quote["subtotal"]

    r = await http_client.post(
        "/api/v1/sales",
        json={
            "employee_id": CASHIER,
            "shift_id": shift_id,
            "customer_id": customer_id,
            "lines": lines,
            "payments": [{"method": "CASH", "amount": quote["total"]}],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["total"] == quote["total"]

    # Tạm tính không ghi gì: không đơn, không sự kiện nào ngoài những gì đơn thật sinh ra.
    assert await edge_db.fetchval("SELECT count(*) FROM sale") == 1


async def test_quote_unknown_product_is_404(http_client: Any, seeded: None) -> None:
    headers = await _token(http_client, CASHIER)
    r = await http_client.post(
        "/api/v1/sales/quote",
        json={"lines": [{"product_id": "KHONG-CO", "quantity": 1}]},
        headers=headers,
    )
    assert r.status_code == 404
