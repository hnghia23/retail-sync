"""`PlaceSale` với Postgres thật — docs/16 §2.

Test đơn vị (`tests/unit/test_place_sale.py`) đã kiểm logic bằng fake. File này kiểm đúng
những thứ **fake không kiểm được**:

  - "MỘT transaction": năm bảng cùng commit, hoặc không bảng nào có gì.
  - Constraint trigger INV-2/INV-3 hoãn tới COMMIT có thật sự cho phép trạng thái trung gian.
  - `event_id` của ledger và của outbox `PointsEarned` là CÙNG một giá trị (docs/12 §3.3).
  - SQL trong adapter chạy được trên schema thật.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

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
    SaleRejectedError,
    place_sale,
)
from shared.config import PricingRules
from shared.db import make_engine, make_session_factory, transaction
from shared.outbox import OutboxPublisher

STORE_ID = "store-001"
CUSTOMER_ID = uuid.UUID("01935abc-0000-7000-8000-000000000001")
OCCURRED_AT = datetime(2026, 9, 17, 3, 12, 44, tzinfo=UTC)


@pytest.fixture
async def edge_stack(edge_db: Any, postgres_dsn: str) -> Any:
    """Session factory SQLAlchemy trỏ vào chính DB mà fixture `edge_db` đã migrate.

    `edge_db` là một connection asyncpg dùng để *kiểm chứng*; `edge_stack` là đường mà
    **code thật** đi. Hai đường riêng biệt là có chủ đích: nếu dùng chung connection, ta sẽ
    đọc thấy dữ liệu chưa commit và test "một transaction" mất hết ý nghĩa.
    """
    dbname = await edge_db.fetchval("SELECT current_database()")
    base = postgres_dsn.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
    engine = make_engine(f"{base}/{dbname}")
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed(db: Any, *, with_customer: bool = True) -> uuid.UUID:
    """Ca đang mở + 3 sản phẩm + (tuỳ chọn) một khách. Không seed quy tắc: mặc định phải chạy."""
    shift_id = uuid.uuid4()
    await db.execute(
        """
        INSERT INTO shift (shift_id, store_id, business_date, opened_by_employee_id, opening_cash)
        VALUES ($1, $2, DATE '2026-09-17', 'emp-007', 500000)
        """,
        shift_id,
        STORE_ID,
    )
    await db.executemany(
        """INSERT INTO product_cache (product_id, sku, barcode, name, unit_price, is_sellable)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        [
            ("SKU-001", "S1", "8930001", "Nuoc mam", 150_000, True),
            ("SKU-002", "S2", "8930002", "Gao", 100_000, True),
            ("SKU-003", "S3", "8930003", "Hang ngung ban", 90_000, False),
        ],
    )
    if with_customer:
        await db.execute(
            """INSERT INTO customer_local (customer_id, phone_hash, tier,
                                           cached_balance, cached_lifetime_earned)
               VALUES ($1, 'hash-1', 'GOLD', 100, 5000)""",
            CUSTOMER_ID,
        )
    return shift_id


async def _run(session_factory: Any, command: PlaceSaleCommand) -> Any:
    """Chạy use case đúng cách tầng route chạy nó: một transaction bao trọn."""
    async with transaction(session_factory) as session:
        outbox = OutboxPublisher(session, store_id=STORE_ID)
        loyalty = await build_loyalty_service(session, store_id=STORE_ID, at=command.occurred_at)
        return await place_sale(
            command,
            catalog=PostgresProductCatalog(session),
            shifts=PostgresShifts(session),
            sales=PostgresSaleRepository(session),
            events=outbox,
            loyalty=loyalty,
            rules=PricingRules(_env_file=None),
        )


def _command(shift_id: uuid.UUID, customer: CustomerSnapshot, **overrides: Any) -> PlaceSaleCommand:
    defaults: dict[str, Any] = {
        "employee_id": "emp-007",
        "shift_id": shift_id,
        "lines": [RequestedLine("SKU-001", 2), RequestedLine("SKU-002", 1)],
        "payments": [RequestedPayment("CASH", 380_000)],
        "customer": customer,
        "occurred_at": OCCURRED_AT,
    }
    return PlaceSaleCommand(**(defaults | overrides))


def _gold_customer() -> CustomerSnapshot:
    return CustomerSnapshot(
        customer_id=CUSTOMER_ID, tier="GOLD", balance=100, lifetime_earned=5_000
    )


# ═══════════════ Một transaction, năm bảng ═══════════════


async def test_sale_writes_all_five_tables_atomically(edge_db: Any, edge_stack: Any) -> None:
    """Khẳng định trung tâm của cả hệ thống (docs/03 §4.1).

    Đếm trên một connection KHÁC, sau khi commit: nếu một bảng thiếu, transaction đã không
    thật sự nguyên tử.
    """
    shift_id = await _seed(edge_db)
    receipt = await _run(edge_stack, _command(shift_id, _gold_customer()))

    assert receipt.total == 380_000  # 400.000 − 5% hạng GOLD
    assert receipt.points_earned == 38

    assert await edge_db.fetchval("SELECT count(*) FROM sale") == 1
    assert await edge_db.fetchval("SELECT count(*) FROM sale_line") == 2
    assert await edge_db.fetchval("SELECT count(*) FROM sale_payment") == 1
    assert await edge_db.fetchval("SELECT count(*) FROM point_ledger_local") == 1
    # Hai sự kiện: SaleCompleted + PointsEarned
    assert await edge_db.fetchval("SELECT count(*) FROM outbox") == 2


async def test_deferred_triggers_allow_intermediate_state(edge_db: Any, edge_stack: Any) -> None:
    """INV-2/INV-3 hoãn tới COMMIT là điều KIỆN CẦN để luồng này chạy được.

    Adapter ghi `sale` trước, rồi mới ghi dòng hàng và thanh toán. Nếu ai đó bỏ
    `DEFERRABLE INITIALLY DEFERRED` trong migration, test này fail ngay — và đó chính là
    cảnh báo sớm cần có, vì lỗi đó làm chết đường bán hàng chứ không chỉ một test.
    """
    shift_id = await _seed(edge_db)
    await _run(edge_stack, _command(shift_id, _gold_customer()))

    row = await edge_db.fetchrow("SELECT subtotal, total FROM sale")
    assert row["subtotal"] == await edge_db.fetchval("SELECT sum(line_total) FROM sale_line")
    assert row["total"] == await edge_db.fetchval("SELECT sum(amount) FROM sale_payment")


async def test_rejected_sale_leaves_no_trace(edge_db: Any, edge_stack: Any) -> None:
    """Ca đã đóng → `409`, và transaction rollback sạch."""
    shift_id = await _seed(edge_db)
    await edge_db.execute(
        "UPDATE shift SET status = 'CLOSED', closed_at = now() WHERE shift_id = $1", shift_id
    )

    with pytest.raises(SaleRejectedError) as exc:
        await _run(edge_stack, _command(shift_id, _gold_customer()))
    assert exc.value.code == "SHIFT_CLOSED"

    assert await edge_db.fetchval("SELECT count(*) FROM sale") == 0
    assert await edge_db.fetchval("SELECT count(*) FROM outbox") == 0
    assert await edge_db.fetchval("SELECT count(*) FROM point_ledger_local") == 0


# ═══════════════ Hợp đồng sự kiện (docs/12) ═══════════════


async def test_ledger_and_outbox_share_the_same_event_id(edge_db: Any, edge_stack: Any) -> None:
    """⚠️ docs/12 §3.3 — nếu hai bên lệch nhau, trung tâm cộng điểm HAI LẦN khi gửi lặp.

    Đây là loại lỗi không gây exception ở đâu cả: mọi thứ trông đúng cho tới khi mạng chập
    chờn và một lô sự kiện được gửi lại.
    """
    shift_id = await _seed(edge_db)
    await _run(edge_stack, _command(shift_id, _gold_customer()))

    ledger_event_id = await edge_db.fetchval("SELECT event_id FROM point_ledger_local")
    outbox_event_id = await edge_db.fetchval(
        "SELECT event_id FROM outbox WHERE event_type = 'PointsEarned'"
    )
    assert ledger_event_id == outbox_event_id


async def test_outbox_payload_carries_trace_context(edge_db: Any, edge_stack: Any) -> None:
    """Ràng buộc #7 — `trace` phải có mặt trong payload, không phải trong HTTP header.

    Độ trễ store→central có thể hàng giờ. Khi tracing chưa bật thì `trace` rỗng, nhưng
    **khóa phải tồn tại**: sync worker và central đọc nó vô điều kiện.
    """
    shift_id = await _seed(edge_db)
    await _run(edge_stack, _command(shift_id, _gold_customer()))

    payload = json.loads(
        await edge_db.fetchval(
            "SELECT payload::text FROM outbox WHERE event_type = 'SaleCompleted'"
        )
    )
    assert "trace" in payload
    assert payload["store_id"] == STORE_ID
    assert payload["schema_version"] == 1
    assert len(payload["payload"]["lines"]) == 2
    assert len(payload["payload"]["payments"]) == 1


async def test_outbox_rows_start_unsent(edge_db: Any, edge_stack: Any) -> None:
    """Sync worker tìm dòng qua partial index `WHERE sent_at IS NULL`."""
    shift_id = await _seed(edge_db)
    await _run(edge_stack, _command(shift_id, _gold_customer()))

    assert await edge_db.fetchval("SELECT count(*) FROM outbox WHERE sent_at IS NULL") == 2
    assert await edge_db.fetchval("SELECT count(*) FROM outbox WHERE attempts <> 0") == 0


# ═══════════════ Quy tắc nghiệp vụ trên dữ liệu thật ═══════════════


async def test_unsellable_product_is_rejected(edge_db: Any, edge_stack: Any) -> None:
    """G7: `is_sellable = false` khiến sản phẩm vắng mặt khỏi `prices_for` → `404`."""
    shift_id = await _seed(edge_db)
    with pytest.raises(SaleRejectedError) as exc:
        await _run(
            edge_stack,
            _command(
                shift_id,
                _gold_customer(),
                lines=[RequestedLine("SKU-003", 1)],
                payments=[RequestedPayment("CASH", 90_000)],
            ),
        )
    assert exc.value.code == "PRODUCT_NOT_FOUND"


async def test_price_is_frozen_from_cache_not_from_request(edge_db: Any, edge_stack: Any) -> None:
    """Giá lấy từ `product_cache`, không từ client.

    Đây là ranh giới tin cậy: client chỉ gửi `product_id` + `quantity`. Cho phép client
    gửi giá là mở đường cho việc bán 1đ một món hàng triệu.
    """
    shift_id = await _seed(edge_db)
    await _run(
        edge_stack,
        _command(
            shift_id,
            CustomerSnapshot.anonymous(),
            lines=[RequestedLine("SKU-001", 1)],
            payments=[RequestedPayment("CASH", 150_000)],
        ),
    )
    assert await edge_db.fetchval("SELECT unit_price FROM sale_line") == 150_000


async def test_anonymous_sale_writes_no_ledger_row(edge_db: Any, edge_stack: Any) -> None:
    """Khách vãng lai: một sự kiện outbox (SaleCompleted), không có dòng ledger."""
    shift_id = await _seed(edge_db, with_customer=False)
    receipt = await _run(
        edge_stack,
        _command(
            shift_id, CustomerSnapshot.anonymous(), payments=[RequestedPayment("CASH", 400_000)]
        ),
    )
    assert receipt.points_earned == 0
    assert await edge_db.fetchval("SELECT count(*) FROM point_ledger_local") == 0
    assert await edge_db.fetchval("SELECT count(*) FROM outbox") == 1


async def test_empty_rule_cache_falls_back_to_defaults(edge_db: Any, edge_stack: Any) -> None:
    """Cửa hàng mới lắp, chưa đồng bộ quy tắc lần nào — VẪN phải bán được.

    `tier_rule_cache` và `earn_rule_cache` rỗng: mặc định 10.000đ = 1 điểm và GOLD giảm 5%
    được dùng thay vì ném lỗi (nguyên tắc kiến trúc #1).
    """
    shift_id = await _seed(edge_db)
    assert await edge_db.fetchval("SELECT count(*) FROM tier_rule_cache") == 0

    receipt = await _run(edge_stack, _command(shift_id, _gold_customer()))
    assert receipt.tier_discount_pct == 5
    assert receipt.points_earned == 38


async def test_synced_tier_rules_override_defaults(edge_db: Any, edge_stack: Any) -> None:
    """Quy tắc là DỮ LIỆU: đồng bộ về ngưỡng khác thì hệ thống theo ngưỡng đó, không sửa code."""
    shift_id = await _seed(edge_db)
    await edge_db.executemany(
        "INSERT INTO tier_rule_cache (tier, min_lifetime_points, discount_pct) VALUES ($1,$2,$3)",
        [("BRONZE", 0, 0), ("GOLD", 1_000, 20)],
    )
    receipt = await _run(
        edge_stack,
        _command(shift_id, _gold_customer(), payments=[RequestedPayment("CASH", 320_000)]),
    )
    assert receipt.tier_discount_pct == 20
    assert receipt.total == 320_000


async def test_split_payment_passes_deferred_invariant(edge_db: Any, edge_stack: Any) -> None:
    """Cổng tuần 2: tiền mặt + thẻ, `SUM(sale_payment) = sale.total` kiểm bởi trigger ở COMMIT."""
    shift_id = await _seed(edge_db)
    await _run(
        edge_stack,
        _command(
            shift_id,
            _gold_customer(),
            payments=[
                RequestedPayment("CASH", 200_000),
                RequestedPayment("CARD", 180_000, "VISA-1234"),
            ],
        ),
    )
    rows = await edge_db.fetch("SELECT seq, method, amount FROM sale_payment ORDER BY seq")
    assert [r["method"] for r in rows] == ["CASH", "CARD"]
    assert sum(r["amount"] for r in rows) == await edge_db.fetchval("SELECT total FROM sale")


async def test_business_date_comes_from_shift(edge_db: Any, edge_stack: Any) -> None:
    """G5 / B02 Z03: ca kéo qua nửa đêm. `business_date` lấy TỪ CA, không từ `occurred_at`."""
    shift_id = await _seed(edge_db)
    await _run(
        edge_stack,
        _command(shift_id, _gold_customer(), occurred_at=datetime(2026, 9, 18, 0, 30, tzinfo=UTC)),
    )
    assert await edge_db.fetchval("SELECT business_date FROM sale") == date(2026, 9, 17)


async def test_catalog_lookup_by_barcode(edge_db: Any, edge_stack: Any) -> None:
    """FR-P03 — quét mã vạch, kèm tuổi bản sao master data để UI cảnh báo (B02 E03)."""
    await _seed(edge_db)
    async with edge_stack() as session:
        product = await PostgresProductCatalog(session).by_barcode("8930001")
        assert product is not None
        assert product["product_id"] == "SKU-001"
        assert product["synced_hours_ago"] < 1
        # Hàng ngừng bán không trả về, y như mã vạch không tồn tại.
        assert await PostgresProductCatalog(session).by_barcode("8930003") is None
