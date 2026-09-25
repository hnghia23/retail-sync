"""Bộ giả lập đầu-cuối — docs/18 §10 bước 2–3: sink `edge_http` + manifest + audit L1/L2.

Chạy bộ giả lập THẬT vào Edge API THẬT (ASGI, Postgres thật), đồng bộ lên Central API THẬT
bằng worker thật, rồi để bộ đối soát kết luận. Đây là phiên bản thu nhỏ của cổng A: một
ngày cửa hàng (2 ca, vài chục đơn, khách mới + khách quen, thanh toán tách) nén còn vài giây.

Hai test cuối làm hỏng dữ liệu có chủ đích: bộ đối soát mà không bao giờ báo DIVERGED thì
không chứng minh được gì.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from edge.sync.worker import run_once
from shared.config import SyncSettings
from simulator.audit import CONVERGED, CONVERGING, DIVERGED, audit
from simulator.generator import plan_store
from simulator.manifest import Manifest
from simulator.profile import Profile
from simulator.sinks.edge_http import StoreTarget, run_edge
from tests.integration.conftest import Central

STORE_ID = "store-001"
PASSWORD = "mat-khau-gia-lap"
SKUS = [f"SKU-{i:03d}" for i in range(1, 9)]
PROFILE = Profile(
    name="it",
    stores=1,
    sales_per_store_day=40,
    identified_share=0.6,
    new_customer_share=0.15,
    shifts_per_day=2,
)
#: 15 giờ mở cửa nén còn ~3 giây thật.
RATE = PROFILE.day_hours * 3600 / 3


@dataclass
class Run:
    manifest: Manifest
    edge_dsn: str
    central_dsn: str
    edge: Any
    central: Central

    async def audit(self) -> Any:
        return await audit(
            self.manifest, edge_dsns={STORE_ID: self.edge_dsn}, central_dsn=self.central_dsn
        )

    async def sync(self) -> None:
        settings = SyncSettings(_env_file=None)
        for _ in range(50):
            report = await run_once(self.edge, self.central.client(), settings=settings)
            assert report.transport_error is None
            if report.claimed == 0:
                return
        raise AssertionError("outbox không cạn sau 50 vòng")


@pytest.fixture
async def run(
    central: Central, edge: Any, edge_db: Any, http_client: Any, postgres_dsn: str
) -> AsyncIterator[Run]:
    from edge.ops.seed import seed_employee_cache
    from shared.db import transaction

    async with transaction(edge) as session:
        await seed_employee_cache(session, store_id=STORE_ID, password=PASSWORD)
    await edge_db.executemany(
        "INSERT INTO product_cache (product_id, sku, barcode, name, unit_price, is_sellable)"
        " VALUES ($1, $1, NULL, $1, $2, true)",
        [(sku, 20_000 * (i + 1)) for i, sku in enumerate(SKUS)],
    )

    today = (datetime.now(UTC) + timedelta(hours=7)).date()
    plan = plan_store(
        PROFILE,
        store_id=STORE_ID,
        catalog=SKUS,
        seed=7,
        nonce=1,
        days=1,
        rate=RATE,
        start_date=today,
    )
    manifest = Manifest(
        run_id="it",
        mode="edge",
        profile=PROFILE.name,
        seed=7,
        nonce=1,
        days=1,
        rate=RATE,
        started_at=datetime.now(UTC).isoformat(),
    )
    await run_edge(
        [StoreTarget(STORE_ID, http_client)],
        {STORE_ID: plan},
        password=PASSWORD,
        manifest=manifest,
        opening_cash=PROFILE.opening_cash,
        lead_seconds=0.1,
    )

    base = postgres_dsn.rsplit("/", 1)[0]
    yield Run(
        manifest=manifest,
        edge_dsn=f"{base}/{await edge_db.fetchval('SELECT current_database()')}",
        central_dsn=f"{base}/{await central.db.fetchval('SELECT current_database()')}",
        edge=edge,
        central=central,
    )


async def test_simulated_day_flows_to_central_and_audit_converges(run: Run) -> None:
    record = run.manifest.stores[STORE_ID]
    # Đáp án có đủ các loại dữ liệu mà luồng phải chở.
    assert len(record.sales) >= 20
    assert len(record.shifts) == 2 and all(s.closed for s in record.shifts.values())
    assert all(s.variance == 0 for s in record.shifts.values())
    assert sum(c["created"] for c in record.customers.values()) >= 2
    assert {m for s in record.sales.values() for m in s.payments} >= {"CASH", "CARD"}
    assert record.rejected == [] and record.unknown == []

    before = await run.audit()
    assert before.status == CONVERGING, before.findings[:3]
    assert before.summary[STORE_ID]["L0"] == before.summary[STORE_ID]["L1"]
    assert before.summary[STORE_ID]["L2"] == {}
    # Độ tươi (docs/17 §6): trung tâm chưa có đơn nào của lần chạy → L2 vắng, không phải 0.
    assert set(before.freshness[STORE_ID]) == {"L1"}

    await run.sync()
    after = await run.audit()
    assert after.status == CONVERGED, after.findings[:5]
    layers = after.summary[STORE_ID]
    assert layers["L0"] == layers["L1"] == layers["L2"]
    assert after.outbox[STORE_ID] == {"pending": 0, "dead": 0}
    fresh = after.freshness[STORE_ID]
    assert set(fresh) == {"L1", "L2"}
    assert fresh["L2"]["behind_seconds"] == 0  # đã bắt kịp: cùng đơn mới nhất với cửa hàng
    assert 0 <= fresh["L1"]["freshness_seconds"] < 3600  # chế độ edge: đồng hồ thật

    # Điểm của khách mới ở trung tâm = đúng tổng điểm các đơn 201 trong đáp án.
    expected = run.manifest.expected_points(STORE_ID)
    balances = {
        str(r["customer_id"]): r["balance"]
        for r in await run.central.db.fetch(
            "SELECT customer_id, balance FROM point_balance WHERE customer_id = ANY($1::uuid[])",
            [uuid.UUID(c) for c in expected],
        )
    }
    assert {c: balances.get(c, 0) for c in expected} == expected


async def test_audit_reports_a_wrong_amount_at_central(run: Run) -> None:
    await run.sync()
    sale_id = next(iter(run.manifest.stores[STORE_ID].sales))
    await run.central.db.execute(
        "UPDATE sale_replica SET total = total + 1 WHERE sale_id = $1", uuid.UUID(sale_id)
    )
    report = await run.audit()
    assert report.status == DIVERGED
    assert [(f.layer, f.ref) for f in report.findings if f.level == DIVERGED] == [
        ("L1→L2", sale_id)
    ]


async def test_audit_reports_a_lost_sale_when_nothing_is_left_to_send(run: Run) -> None:
    """Thiếu ở trung tâm mà outbox cửa hàng đã cạn = MẤT dữ liệu, không phải "đang chảy"."""
    await run.sync()
    sale_id = next(iter(run.manifest.stores[STORE_ID].sales))
    await run.central.db.execute("DELETE FROM point_ledger WHERE sale_id = $1", uuid.UUID(sale_id))
    await run.central.db.execute("DELETE FROM sale_replica WHERE sale_id = $1", uuid.UUID(sale_id))

    report = await run.audit()
    assert report.status == DIVERGED
    assert any(f.what == "thiếu đơn" and f.ref == sale_id for f in report.findings)


# ═══════════ L3 — bronze trong ClickHouse (giai đoạn A, docs/17 §5) ═══════════


@pytest.fixture
async def warehouse(run: Run, minio: Any, ch_db: Any) -> AsyncIterator[Any]:
    """Pipeline S4/S5 trỏ vào đúng DB trung tâm mà `run` vừa đồng bộ lên."""
    from pipeline.clickhouse import ClickHouse
    from pipeline.config import LakeConfig, PipelineConfig
    from pipeline.lake import Lake

    lake_cfg = LakeConfig(
        endpoint=minio.endpoint,
        access_key=minio.access_key,
        secret_key=minio.secret_key,
        url_for_clickhouse=minio.url_for_clickhouse,
        bucket=f"lake-{uuid.uuid4().hex[:8]}",
    )
    cfg = PipelineConfig(
        pg_dsn=run.central_dsn,
        lake=lake_cfg,
        clickhouse=ch_db,
        window_seconds=1,
        safety_lag_seconds=0,
    )
    lake = Lake(lake_cfg)
    lake.ensure_bucket()
    ch = ClickHouse(ch_db)
    try:
        yield cfg, lake, ch
    finally:
        ch.close()


def _target(ch_db: Any) -> Any:
    from simulator.audit import ClickHouseTarget

    return ClickHouseTarget(
        url=ch_db.url + "/", user=ch_db.user, password=ch_db.password, database=ch_db.database
    )


async def test_simulated_day_reaches_bronze_and_all_four_layers_agree(
    run: Run, warehouse: Any, ch_db: Any
) -> None:
    """Chặng S1 → S5 đi hết, bằng dữ liệu của bộ giả lập: L0 = L1 = L2 = L3 từng con số."""
    import asyncio

    from pipeline.run import run_once as pipeline_once

    cfg, lake, ch = warehouse
    await run.sync()
    before = await audit(
        run.manifest,
        edge_dsns={STORE_ID: run.edge_dsn},
        central_dsn=run.central_dsn,
        clickhouse=_target(ch_db),
    )
    assert before.status == CONVERGING  # bronze chưa có gì: chưa tới, không phải mất
    assert all(f.layer == "L2→L3" for f in before.findings)

    await asyncio.sleep(1.2)  # qua mép cửa sổ 1 giây chứa những dòng cuối cùng
    await pipeline_once(cfg, lake, ch)
    after = await audit(
        run.manifest,
        edge_dsns={STORE_ID: run.edge_dsn},
        central_dsn=run.central_dsn,
        clickhouse=_target(ch_db),
    )
    assert after.status == CONVERGED, after.findings[:5]
    layers = after.summary[STORE_ID]
    assert layers["L0"] == layers["L1"] == layers["L2"] == layers["L3"]

    # Chạy lại pipeline (AT-07) → vẫn khớp, không nhân đôi.
    await pipeline_once(cfg, lake, ch)
    again = await audit(
        run.manifest,
        edge_dsns={STORE_ID: run.edge_dsn},
        central_dsn=run.central_dsn,
        clickhouse=_target(ch_db),
    )
    assert again.status == CONVERGED, again.findings[:5]


async def test_audit_reports_a_sale_lost_between_central_and_bronze(
    run: Run, warehouse: Any, ch_db: Any
) -> None:
    """Đơn nằm dưới mép "đã nạp tới" mà vắng ở bronze = MẤT ở S4/S5, không phải "chưa tới"."""
    import asyncio

    from pipeline.run import run_once as pipeline_once

    cfg, lake, ch = warehouse
    await run.sync()
    await asyncio.sleep(1.2)
    await pipeline_once(cfg, lake, ch)

    sale_id = next(iter(run.manifest.stores[STORE_ID].sales))
    ch.query(
        f"ALTER TABLE bronze_sale DELETE WHERE sale_id = toUUID('{sale_id}')",
        mutations_sync=1,
    )
    report = await audit(
        run.manifest,
        edge_dsns={STORE_ID: run.edge_dsn},
        central_dsn=run.central_dsn,
        clickhouse=_target(ch_db),
    )
    assert report.status == DIVERGED
    assert any(
        f.layer == "L2→L3" and f.what == "thiếu đơn" and f.ref == sale_id and f.level == DIVERGED
        for f in report.findings
    )


# ═══════════ L4 — mart của dbt (giai đoạn A, docs/17 §5) ═══════════


async def test_simulated_day_reaches_the_marts_and_all_five_layers_agree(
    run: Run, warehouse: Any, ch_db: Any, dbt: Any
) -> None:
    """Chặng S1 → S6 đi hết bằng dữ liệu của bộ giả lập: L0 = L1 = L2 = L3 = L4 từng con số,
    và dbt chạy lại (AT-07) không đổi gì."""
    import asyncio

    from pipeline.run import run_once as pipeline_once

    cfg, lake, ch = warehouse
    await run.sync()
    await asyncio.sleep(1.2)
    await pipeline_once(cfg, lake, ch)

    def check() -> Any:
        return audit(
            run.manifest,
            edge_dsns={STORE_ID: run.edge_dsn},
            central_dsn=run.central_dsn,
            clickhouse=_target(ch_db),
            marts=True,
        )

    before = await check()
    assert before.status == CONVERGING  # chưa có mart: chưa tới, không phải mất
    assert {f.layer for f in before.findings} == {"L3→L4"}

    result = dbt.build()
    assert result.returncode == 0, result.stdout[-4000:]
    after = await check()
    assert after.status == CONVERGED, after.findings[:5]
    layers = after.summary[STORE_ID]
    assert layers["L0"] == layers["L1"] == layers["L2"] == layers["L3"] == layers["L4"]
    # Bắt kịp ở MỌI tầng — kể cả khi `occurred_at` đi qua Parquet và ClickHouse (DateTime64(6)).
    fresh = after.freshness[STORE_ID]
    assert set(fresh) == {"L1", "L2", "L3", "L4"}
    assert {layer: f["behind_seconds"] for layer, f in fresh.items()} == dict.fromkeys(fresh, 0)

    assert dbt.build().returncode == 0
    assert (await check()).status == CONVERGED


async def test_audit_reports_a_sale_lost_between_bronze_and_the_marts(
    run: Run, warehouse: Any, ch_db: Any, dbt: Any
) -> None:
    """Đơn có ở bronze, `recorded_at` dưới mép mart đã dựng tới, mà vắng ở mart = MẤT ở S6."""
    import asyncio

    from pipeline.run import run_once as pipeline_once

    cfg, lake, ch = warehouse
    await run.sync()
    await asyncio.sleep(1.2)
    await pipeline_once(cfg, lake, ch)
    assert dbt.build().returncode == 0

    sale_id = next(iter(run.manifest.stores[STORE_ID].sales))
    for table in ("fact_sale_line", "fact_payment"):
        ch.query(
            f"ALTER TABLE {table} DELETE WHERE sale_id = toUUID('{sale_id}')", mutations_sync=1
        )
    report = await audit(
        run.manifest,
        edge_dsns={STORE_ID: run.edge_dsn},
        central_dsn=run.central_dsn,
        clickhouse=_target(ch_db),
        marts=True,
    )
    assert report.status == DIVERGED
    assert any(
        f.layer == "L3→L4" and f.what == "thiếu đơn" and f.ref == sale_id and f.level == DIVERGED
        for f in report.findings
    )
