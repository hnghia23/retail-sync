"""AT-01…04 trên compose thật, dữ liệu do bộ giả lập sinh (docs/01 §AT, docs/16 §2).

Mỗi kịch bản là một lịch ý định viết tay (không ngẫu nhiên) đưa qua ĐÚNG sink mà lần chạy thật
dùng, rồi kết luận bằng ĐÚNG bộ đối soát (`simulator audit`). Điều kiện đạt chung: `CONVERGED`.
"""
# SQL chỉ ghép hằng của test.

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from edge.loyalty.domain.points import EarnRule, points_earned_for
from edge.sync.client import HttpCentralClient
from shared.config import SyncSettings
from shared.seed_data import parse_products
from simulator.audit import CONVERGED, CONVERGING, audit, audit_until_settled
from simulator.generator import CloseShift, CustomerRef, Intent, OpenShift, Sale
from simulator.manifest import Manifest
from simulator.profile import load_profile
from simulator.quirks import Quirks
from simulator.sinks.edge_http import StoreTarget, run_edge
from simulator.sinks.virtual import read_keys, run_virtual
from tests.scenarios.conftest import CRAWL_DATA, STORE, VIRTUAL_KEYS, Stack, container

PRODUCTS = {p.product_id: p.unit_price for p in parse_products(
    CRAWL_DATA / "products" / "product_details.csv"
)[:40]}  # fmt: skip
#: Giá ≥ 10.000đ để đơn một dòng chắc chắn có điểm (10.000đ = 1 điểm, FR-L03).
PID = next(p for p, price in PRODUCTS.items() if price >= 20_000)


def _customer(tag: str) -> CustomerRef:
    return CustomerRef(
        ref=f"{tag}-{uuid.uuid4().hex[:6]}", new=True, phone=f"09{secrets.randbelow(10**8):08d}"
    )


def _plan(sales_at: list[float], customer: CustomerRef | None, *, close_at: float) -> list[Intent]:
    """Mở ca → các đơn (đơn đầu đăng ký khách mới, đơn sau là khách cũ) → đóng ca."""
    intents: list[Intent] = [OpenShift(at=0.0, day=0, shift_no=0)]
    for i, at in enumerate(sales_at):
        c = (
            customer
            if (customer is None or i == 0)
            else CustomerRef(customer.ref, False, customer.phone)
        )
        intents.append(
            Sale(at=at, intent_id=f"s{i}", lines=((PID, 1 + i % 3),), customer=c, payment="CASH")
        )
    intents.append(CloseShift(at=close_at, day=0, shift_no=0))
    return intents


async def _run_edge(stack: Stack, run_id: str, plan: list[Intent]) -> Manifest:
    manifest = Manifest(
        run_id=f"{run_id}-{uuid.uuid4().hex[:6]}",
        mode="edge",
        profile="scenario",
        seed=0,
        nonce=0,
        days=1,
        rate=1.0,
        started_at=datetime.now(UTC).isoformat(),
    )
    async with httpx.AsyncClient(base_url=stack.edge_url, timeout=15) as client:
        await run_edge(
            [StoreTarget(STORE, client)],
            {STORE: plan},
            password=stack.password,
            manifest=manifest,
            opening_cash=500_000,
        )
    return manifest


def _audit_args(stack: Stack) -> dict[str, Any]:
    return {"edge_dsns": {STORE: stack.edge_dsn}, "central_dsn": stack.central_dsn}


# ═══════════════ AT-01 ═══════════════


async def test_normal_sale_with_known_customer(stack: Stack) -> None:
    """AT-01 — đơn có nhận diện khách: lưu đủ, điểm tăng đúng công thức, lên trung tâm đúng."""
    customer = _customer("at01")
    manifest = await _run_edge(stack, "at01", _plan([1.0, 2.0, 3.0], customer, close_at=4.5))

    sales = list(manifest.stores[STORE].sales.values())
    assert len(sales) == 3 and not manifest.stores[STORE].unknown
    for sale in sales:
        assert sale.customer_id is not None
        assert sale.points == points_earned_for(
            sale.total, EarnRule()
        )  # 10.000đ = 1 điểm, làm tròn xuống

    report = await audit_until_settled(manifest, wait_seconds=120, **_audit_args(stack))
    assert report.status == CONVERGED, report.findings[:5]


# ═══════════════ AT-02 ═══════════════


async def test_offline_then_reconnect_ten_sales(stack: Stack) -> None:
    """AT-02 — mất kết nối tới trung tâm, bán 10 đơn, nối lại: 10 đơn lên đủ, điểm cộng đúng một
    lần, và lỗi đường truyền KHÔNG đốt lượt thử của sự kiện nào (quy tắc của worker, CLAUDE.md)."""
    customer = _customer("at02")
    stack.docker("stop", container("central-api"))
    try:
        manifest = await _run_edge(
            stack, "at02", _plan([1.0 + i * 0.5 for i in range(10)], customer, close_at=7.0)
        )
        assert len(manifest.stores[STORE].sales) == 10  # cửa hàng tự chủ: bán được khi mất mạng

        edge = await asyncpg.connect(stack.edge_dsn)
        try:
            pending, max_attempts = await edge.fetchrow(
                "SELECT count(*), COALESCE(max(attempts), 0) FROM outbox"
                " WHERE sent_at IS NULL AND dead_lettered_at IS NULL"
            )
        finally:
            await edge.close()
        assert pending >= 10
        assert max_attempts == 0, "lỗi đường truyền đã bị tính là một lượt thử (AT-02)"

        during = await audit(manifest, **_audit_args(stack))
        assert during.status == CONVERGING  # chưa tới, không phải mất
    finally:
        stack.docker("start", container("central-api"))

    # Worker lùi tối đa SYNC_BACKOFF_MAX_SECONDS giữa hai lần thử khi còn mất mạng.
    report = await audit_until_settled(manifest, wait_seconds=420, **_audit_args(stack))
    assert report.status == CONVERGED, report.findings[:5]

    central = await asyncpg.connect(stack.central_dsn)
    try:
        cid = next(iter(manifest.stores[STORE].customers))
        rows, balance = await central.fetchrow(
            "SELECT count(*), COALESCE(sum(delta), 0) FROM point_ledger WHERE customer_id = $1",
            uuid.UUID(cid),
        )
    finally:
        await central.close()
    earned = [s.points for s in manifest.stores[STORE].sales.values() if s.points]
    assert (rows, balance) == (len(earned), sum(earned))  # đúng một lần mỗi đơn


# ═══════════════ AT-03 ═══════════════


async def test_duplicate_sync_event_five_times(stack: Stack) -> None:
    """AT-03 — gửi lặp cùng một sự kiện đồng bộ 5 lần: trung tâm nhận cả 5, điểm cộng một lần."""
    manifest = await _run_edge(stack, "at03", _plan([1.0], _customer("at03"), close_at=2.5))
    assert (
        await audit_until_settled(manifest, wait_seconds=120, **_audit_args(stack))
    ).status == CONVERGED
    sale_id = next(iter(manifest.stores[STORE].sales))
    cid = uuid.UUID(next(iter(manifest.stores[STORE].customers)))

    edge = await asyncpg.connect(stack.edge_dsn)
    try:
        rows = await edge.fetch(
            "SELECT payload FROM outbox WHERE event_type IN ('SaleCompleted', 'PointsEarned')"
            " AND payload->'payload'->>'sale_id' = $1",
            sale_id,
        )
    finally:
        await edge.close()
    import json

    envelopes = [
        json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"] for r in rows
    ]
    assert {e["event_type"] for e in envelopes} == {"SaleCompleted", "PointsEarned"}

    central = await asyncpg.connect(stack.central_dsn)
    client = HttpCentralClient(
        base_url=stack.central_url, api_key=stack.store_key, timeout_seconds=10
    )
    try:
        before = await central.fetchval(
            "SELECT balance FROM point_balance WHERE customer_id = $1", cid
        )
        for _ in range(5):
            result = await client.push(envelopes)
            assert {str(e) for e in result.accepted} == {e["event_id"] for e in envelopes}
            assert result.rejected == []
        ids = [uuid.UUID(e["event_id"]) for e in envelopes]
        assert (
            await central.fetchval(
                "SELECT count(*) FROM point_ledger WHERE event_id = ANY($1::uuid[])", ids
            )
            == 1
        )
        assert (
            await central.fetchval(
                "SELECT count(*) FROM sale_replica WHERE sale_id = $1", uuid.UUID(sale_id)
            )
            == 1
        )
        assert (
            await central.fetchval("SELECT balance FROM point_balance WHERE customer_id = $1", cid)
            == before
        )
    finally:
        await client.aclose()
        await central.close()
    assert (await audit(manifest, **_audit_args(stack))).status == CONVERGED


# ═══════════════ AT-04 ═══════════════


async def test_concurrent_purchase_two_stores(stack: Stack) -> None:
    """AT-04 — cùng một khách mua ĐỒNG THỜI ở hai cửa hàng: số dư = tổng cả hai, không mất dòng.

    Chạy bằng chế độ `virtual` (docs/16 §2 cho phép): ở chế độ `edge`, cửa hàng 2 chưa có cách
    biết khách đăng ký ở cửa hàng 1 — tra khách qua trung tâm là tính năng C (AT-05)."""
    keys_file = VIRTUAL_KEYS
    if not keys_file.exists():
        pytest.fail(f"thiếu {keys_file} — chạy make provision-virtual (và seed nhân viên)")
    home, other = read_keys(keys_file.read_text(encoding="utf-8"))[-2:]
    customer = _customer("at04")
    returning = CustomerRef(customer.ref, False, customer.phone)
    n = 8
    burst = 4.0  # cả hai cửa hàng bán cho CÙNG khách ở CÙNG thời điểm

    def sales(sid: str, c: CustomerRef) -> list[Intent]:
        return [
            Sale(
                at=burst,
                intent_id=f"{sid}-{i}",
                lines=((PID, 1 + i % 3),),
                customer=c,
                payment="CASH",
            )
            for i in range(n)
        ]

    plans: dict[str, list[Intent]] = {
        home.store_id: [
            OpenShift(at=0.0, day=0, shift_no=0),
            Sale(at=0.5, intent_id="reg", lines=((PID, 1),), customer=customer, payment="CASH"),
            *sales(home.store_id, returning),
            CloseShift(at=7.0, day=0, shift_no=0),
        ],
        other.store_id: [
            OpenShift(at=0.0, day=0, shift_no=0),
            *sales(other.store_id, returning),
            CloseShift(at=7.0, day=0, shift_no=0),
        ],
    }
    manifest = Manifest(
        run_id=f"at04-{uuid.uuid4().hex[:6]}", mode="virtual", profile="scenario", seed=0,
        nonce=0, days=1, rate=1.0, started_at=datetime.now(UTC).isoformat(),
    )  # fmt: skip
    yesterday = (datetime.now(UTC) + timedelta(hours=7)).date() - timedelta(days=1)
    await run_virtual(
        [home, other],
        plans,
        central_url=stack.central_url,
        manifest=manifest,
        profile=load_profile("t0"),
        start_date=yesterday,
        rate=1.0,
        prices=PRODUCTS,
        quirks=Quirks(),
        settings=SyncSettings(poll_interval_seconds=0.1, backoff_max_seconds=2.0),
        drain_timeout=120,
    )
    stats = manifest.stores[other.store_id].outbox
    assert (stats["foreign_customer_sales"], stats["foreign_fallback"]) == (n, 0)
    assert all(r.outbox["pending"] == 0 and r.outbox["dead"] == 0 for r in manifest.stores.values())

    cid = uuid.UUID(next(iter(manifest.stores[home.store_id].customers)))
    expected = sum(
        s.points
        for r in manifest.stores.values()
        for s in r.sales.values()
        if s.customer_id == str(cid)
    )
    central = await asyncpg.connect(stack.central_dsn)
    try:
        balance, lifetime = await central.fetchrow(
            "SELECT balance, lifetime_earned FROM point_balance WHERE customer_id = $1", cid
        )
        ledger_rows, ledger_sum = await central.fetchrow(
            "SELECT count(*), sum(delta) FROM point_ledger WHERE customer_id = $1", cid
        )
    finally:
        await central.close()
    point_sales = sum(1 for r in manifest.stores.values() for s in r.sales.values()
                      if s.customer_id == str(cid) and s.points)  # fmt: skip
    assert balance == lifetime == ledger_sum == expected
    assert ledger_rows == point_sales  # không mất dòng nào của cửa hàng nào

    report = await audit(manifest, edge_dsns={}, central_dsn=stack.central_dsn)
    assert report.status == CONVERGED, report.findings[:5]
