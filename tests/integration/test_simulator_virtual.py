"""Chế độ `virtual` trên Central API THẬT (ASGI) + Postgres thật — docs/18 §3, §5.

Ba cửa hàng ảo, hai ngày vắt qua cuối tháng, bật cả ba tật của cổng A: một cửa hàng offline
qua đêm cuối tháng trước → ngày 1 tháng này, lô đã nhận bị gửi lại, khách mua chéo cửa hàng.
Đáp án là envelope bộ giả lập đã dựng; bộ đối soát phải ra `CONVERGED` ở L0→L2 từng đơn, từng
điểm.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from shared.config import SyncSettings
from simulator.audit import CONVERGED, audit
from simulator.generator import plan_store
from simulator.manifest import Manifest
from simulator.profile import load_profile
from simulator.quirks import Quirks, mark_concurrent_customers, offline_stores
from simulator.sinks.virtual import VirtualTarget, run_virtual
from tests.integration.conftest import Central

CATALOG = {f"P{i:03d}": 5_000 + 1_500 * i for i in range(80)}
DAYS = 2
# Ngày cuối của THÁNG TRƯỚC, tính theo hôm nay — không bao giờ là một ngày cố định. Partition
# `point_ledger` chỉ giữ tháng trước + 3 tháng tới (migration 0006): `date(2026, 8, 31)` cố định
# chạy xanh tới hết tháng 9 rồi đỏ từ 1/10 (điểm ngày 31/8 không còn partition → dead-letter).
START = datetime.now(UTC).date().replace(day=1) - timedelta(days=1)
EXTRA_STORE = "store-v03"


async def _targets(central: Central) -> list[VirtualTarget]:
    """Hai cửa hàng của fixture + một cửa hàng cấp khóa bằng CHÍNH lệnh ops."""
    from central.ops.provision_store import provision_store
    from central.settings import get_settings
    from shared.db import make_engine, make_session_factory, transaction

    engine = make_engine(get_settings().database_url)
    try:
        async with transaction(make_session_factory(engine)) as session:
            extra = await provision_store(session, store_id=EXTRA_STORE, name=EXTRA_STORE)
    finally:
        await engine.dispose()
    return [VirtualTarget(sid, key) for sid, key in {**central.keys, EXTRA_STORE: extra}.items()]


async def _run(
    central: Central,
    quirks: Quirks,
    *,
    settings: SyncSettings,
    offline: list[str] | None = None,
) -> Manifest:
    profile = dataclasses.replace(load_profile("t0"), sales_per_store_day=40)
    rate = profile.day_hours * 3600 * DAYS / 8  # hai ngày giả lập trong ~8 giây
    targets = await _targets(central)
    store_ids = [t.store_id for t in targets]
    plans = mark_concurrent_customers(
        {
            sid: plan_store(
                profile,
                store_id=sid,
                catalog=list(CATALOG),
                prices=CATALOG,
                seed=11,
                nonce=uuid.uuid4().int % 10**8,
                days=DAYS,
                rate=rate,
                start_date=START,
            )
            for sid in store_ids
        },
        quirks,
        seed=11,
    )
    manifest = Manifest(
        run_id="virtual-test",
        mode="virtual",
        profile=profile.name,
        seed=11,
        nonce=0,
        days=DAYS,
        rate=rate,
        started_at="",
    )
    await run_virtual(
        targets,
        plans,
        central_url="http://central-under-test",
        manifest=manifest,
        profile=profile,
        start_date=START,
        rate=rate,
        prices=CATALOG,
        quirks=quirks,
        offline_store_ids=offline
        if offline is not None
        else offline_stores(store_ids, quirks, seed=11),
        settings=settings,
        drain_timeout=120,
        seed=11,
        transport=httpx.ASGITransport(app=central.app),
    )
    return manifest


def _dsn(central: Central, postgres_dsn: str, dbname: str) -> str:
    return postgres_dsn.rsplit("/", 1)[0] + f"/{dbname}"


FAST = SyncSettings(
    batch_size=50,
    poll_interval_seconds=0.05,
    backoff_max_seconds=0.3,
    # Cố ý rất thấp: nếu lỗi ĐƯỜNG TRUYỀN (offline) bị tính là một lượt thử thì cả ngày bán
    # của cửa hàng offline vào dead-letter — đúng lỗi AT-02 mà quy tắc của worker cấm.
    max_attempts_before_dead_letter=2,
    request_timeout_seconds=10,
)


async def test_virtual_stores_with_all_gate_a_quirks_converge_at_central(
    central: Central, postgres_dsn: str
) -> None:
    quirks = Quirks(
        offline=True,
        offline_share=0.34,  # 1 trong 3 cửa hàng
        resend=True,
        resend_share=1.0,  # MỌI lô đã nhận bị gửi lại
        concurrent_customer=True,
        concurrent_share=0.3,
    )
    manifest = await _run(central, quirks, settings=FAST)
    stats = {sid: rec.outbox for sid, rec in manifest.stores.items()}

    assert all(s["pending"] == 0 and s["dead"] == 0 for s in stats.values()), stats
    offline_ids = offline_stores(sorted(stats), quirks, seed=11)
    assert all(stats[sid]["transport_errors"] > 0 for sid in offline_ids)  # tật có tác dụng
    assert sum(s["resent_batches"] for s in stats.values()) > 0
    assert sum(s["resend_mismatch"] for s in stats.values()) == 0  # CH-7: idempotent
    assert sum(s["foreign_customer_sales"] for s in stats.values()) > 0  # AT-04 có xảy ra

    # Vắt qua cuối tháng: có đơn cả hai tháng, ngày kinh doanh đúng theo giờ cửa hàng.
    days = {s.business_date for r in manifest.stores.values() for s in r.sales.values()}
    assert days == {START.isoformat(), (START + timedelta(days=1)).isoformat()}

    dbname = await central.db.fetchval("SELECT current_database()")
    report = await audit(manifest, edge_dsns={}, central_dsn=_dsn(central, postgres_dsn, dbname))
    assert report.status == CONVERGED, report.findings[:5]
    for layers in report.summary.values():
        assert layers["L0"] == layers["L1"] == layers["L2"]

    # INV-4 cho khách mua chéo cửa hàng: số dư = Σ sổ cái, không mất lượt cộng nào.
    drift = await central.db.fetchval(
        "SELECT count(*) FROM point_balance b WHERE b.balance <>"
        " (SELECT COALESCE(sum(delta), 0) FROM point_ledger l WHERE l.customer_id = b.customer_id)"
    )
    assert drift == 0


async def test_audit_catches_a_sale_the_virtual_store_sent_but_central_lost(
    central: Central, postgres_dsn: str
) -> None:
    """Đối chứng: bộ đối soát ở chế độ virtual không phải lúc nào cũng xanh."""
    manifest = await _run(central, Quirks(), settings=FAST, offline=[])
    sale_id = next(iter(next(iter(manifest.stores.values())).sales))
    await central.db.execute("DELETE FROM sale_replica WHERE sale_id = $1", uuid.UUID(sale_id))

    dbname = await central.db.fetchval("SELECT current_database()")
    report = await audit(manifest, edge_dsns={}, central_dsn=_dsn(central, postgres_dsn, dbname))
    assert report.status != CONVERGED
    assert any(f.layer == "L1→L2" and f.ref == sale_id for f in report.findings)


@pytest.fixture(autouse=True)
def _pii_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PII_HASH_KEY", "test-virtual")


def _unused(*_: Any) -> None:  # giữ import Any cho kiểu fixture
    return None
