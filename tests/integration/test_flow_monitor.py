"""Bộ giám sát luồng (`pipeline.monitor`) trên Postgres + MinIO + ClickHouse + dbt THẬT.

Dashboard "sức khỏe luồng" (docs/17 §6) chỉ đáng tin bằng các con số này. Mỗi test dựng một
trạng thái đã biết của luồng rồi kiểm con số đọc được, gồm cả những tình huống mà dashboard phải
thấy trước khi dữ liệu kịp sai:
  - transaction treo làm trích xuất đứng lại (bẫy 1: an toàn, nhưng phải THẤY);
  - đơn đã nạp mà dbt chưa dựng (S6 tụt lại) rồi bắt kịp;
  - một nguồn chết không kéo chết cả lần đo, và không báo 0 giả.
"""
# SQL trong file này chỉ ghép hằng của chính test.
# ruff: noqa: S608

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from pipeline.clickhouse import ClickHouse
from pipeline.lake import Lake
from pipeline.monitor import collect
from pipeline.tables import INCREMENTAL
from tests.integration.conftest import Dbt, Env
from tests.integration.test_pipeline import STORE, _sale, _seed, _settle


async def _sync_status(db: Any, *, store: str = STORE, seconds_ago: float = 0) -> None:
    await db.execute(
        "INSERT INTO store_sync_status (store_id, last_event_at, lag_seconds, status, updated_at)"
        " VALUES ($1, now() - make_interval(secs => $2), 3, 'OK',"
        " now() - make_interval(secs => $2))"
        " ON CONFLICT (store_id) DO UPDATE SET last_event_at = EXCLUDED.last_event_at,"
        " updated_at = EXCLUDED.updated_at",
        store,
        float(seconds_ago),
    )


async def test_empty_flow_reports_sources_up_and_no_fake_stage_numbers(env: Env) -> None:
    """Chưa có gì chảy qua: ba nguồn đều `up`, và các mép CHƯA TỒN TẠI thì vắng mặt — không
    phải 0 (0 trông như "không trễ")."""
    health = await collect(env.cfg, env.lake, env.ch)

    assert health.errors == {}
    for source in ("central_pg", "lake", "clickhouse"):
        assert health.value("flow_monitor_source_up", source=source) == 1
    assert health.value("reconcile_drift_count") == 0
    assert health.value("point_ledger_partition_months_ahead") == 3  # migration tạo sẵn 3 tháng
    assert health.value("pipeline_loaded_until_age_seconds") is None
    assert not [s for s in health.samples if s.name == "pipeline_extract_watermark_age_seconds"]
    # safety_lag = 0 và không có transaction nào khác đang mở: mép gần như là "bây giờ".
    lag = health.value("pipeline_extract_horizon_lag_seconds")
    assert lag is not None and lag < 5


async def test_watermarks_follow_the_pipeline_and_s3_numbers_come_from_central(
    env: Env,
) -> None:
    await _seed(env.db)
    await _sync_status(env.db, seconds_ago=120)
    await env.db.execute(
        "INSERT INTO dead_letter_event (event_id, store_id, event_type, payload, error)"
        f" VALUES ($1, '{STORE}', 'SaleCompleted', '{{}}', 'invalid_payload: x')",
        uuid.uuid4(),
    )
    await _settle()
    await env.run()

    health = await collect(env.cfg, env.lake, env.ch)

    assert health.errors == {}
    freshness = health.value("flow_freshness_seconds", layer="L2", store_id=STORE)
    assert freshness is not None and 110 < freshness < 200
    assert health.value("store_sync_lag_seconds", store_id=STORE) == 3
    assert health.value("dead_letter_events", store_id=STORE) == 1

    extract = {
        dict(s.labels)["table"]: s.value
        for s in health.samples
        if s.name == "pipeline_extract_watermark_age_seconds"
    }
    load = {
        dict(s.labels)["table"]: s.value
        for s in health.samples
        if s.name == "pipeline_load_watermark_age_seconds"
    }
    # Mọi bảng incremental có mép — kể cả bảng không có dòng mới nhờ file mốc (docs/17 §4).
    assert set(extract) == set(load) == {spec.name for spec in INCREMENTAL}
    # Một lượt nạp hết những gì nó trích: hai mép trùng nhau từng bảng.
    for table, age in extract.items():
        assert abs(age - load[table]) < 0.01, table
    until = health.value("pipeline_loaded_until_age_seconds")
    assert until is not None and until == max(load.values())
    # dbt chưa chạy lần nào → chưa có fact: S6 và L4 vắng mặt, không báo "0 đơn chờ".
    assert health.value("pipeline_transform_pending_sales") is None


async def test_stuck_transaction_shows_up_as_extract_horizon_lag(
    env: Env, postgres_dsn: str
) -> None:
    """Bẫy 1: một transaction mở lâu giữ `extract_horizon()` đứng lại. Không mất dữ liệu, nhưng
    luồng ngừng tiến — và đây là chỉ số duy nhất nói được VÌ SAO nó ngừng."""
    blocker = await asyncpg.connect(env.cfg.pg_dsn)
    try:
        tx = blocker.transaction()
        await tx.start()
        await blocker.execute("SELECT 1")
        await asyncio.sleep(2.5)
        health = await collect(env.cfg, env.lake, env.ch)
        await tx.rollback()
    finally:
        await blocker.close()

    lag = health.value("pipeline_extract_horizon_lag_seconds")
    assert lag is not None and lag >= 2.4


async def test_transform_lag_rises_when_dbt_falls_behind_and_returns_to_zero(
    env: Env, dbt: Dbt
) -> None:
    """S6 bằng dbt THẬT: đơn nạp xong mà dbt chưa chạy → `pending` > 0 và tuổi tăng; dbt chạy
    xong → về 0, và độ tươi L4 của cửa hàng xuất hiện qua `dim_store` như người phân tích thấy."""
    await _seed(env.db, sales=3)
    await _settle()
    await env.run()
    built = dbt.build()
    assert built.returncode == 0, built.stdout[-3000:]

    caught_up = await collect(env.cfg, env.lake, env.ch)
    assert caught_up.value("pipeline_transform_pending_sales") == 0
    assert caught_up.value("pipeline_transform_lag_seconds") == 0
    l4 = caught_up.value("flow_freshness_seconds", layer="L4", store_id=STORE)
    assert l4 is not None and l4 < 120

    # Hai đơn mới qua S4/S5 nhưng dbt KHÔNG chạy.
    shift = await env.db.fetchval("SELECT shift_id FROM shift_replica LIMIT 1")
    await _sale(env.db, shift, amount=10_000)
    await _sale(env.db, shift, amount=20_000)
    await _settle()
    await env.run()

    behind = await collect(env.cfg, env.lake, env.ch)
    assert behind.value("pipeline_transform_pending_sales") == 2
    lag = behind.value("pipeline_transform_lag_seconds")
    assert lag is not None and lag > 1

    assert dbt.build().returncode == 0
    again = await collect(env.cfg, env.lake, env.ch)
    assert again.value("pipeline_transform_pending_sales") == 0
    assert again.value("pipeline_transform_lag_seconds") == 0


async def test_dead_source_is_reported_down_without_hiding_the_others(env: Env) -> None:
    await _seed(env.db)
    await _sync_status(env.db)
    dead = ClickHouse(
        dataclasses.replace(env.cfg.clickhouse, url="http://127.0.0.1:1")  # không có gì nghe
    )
    try:
        health = await collect(env.cfg, env.lake, dead)
    finally:
        dead.close()

    assert health.value("flow_monitor_source_up", source="clickhouse") == 0
    assert "clickhouse" in health.errors
    assert health.value("pipeline_loaded_until_age_seconds") is None
    # Nguồn khác vẫn đo bình thường.
    assert health.value("flow_monitor_source_up", source="central_pg") == 1
    assert health.value("flow_freshness_seconds", layer="L2", store_id=STORE) is not None


async def test_latest_files_matches_a_full_listing(env: Env) -> None:
    """`Lake.latest_files` (chỉ đọc phân vùng mới nhất) phải cho CÙNG mép cuối với liệt kê toàn
    bộ lake — kể cả khi lake trải qua nhiều ngày `dt=`."""
    lake: Lake = env.lake
    base = datetime(2026, 8, 30, 22, tzinfo=UTC)
    for hours in range(6):  # 30/8 22:00 → 31/8 04:00: hai phân vùng dt=
        start = base + timedelta(hours=hours)
        end = start + timedelta(hours=1)
        name = f"{start:%Y%m%dT%H%M%SZ}_{end:%Y%m%dT%H%M%SZ}.parquet"
        lake.put(lake.key("sale", start.date().isoformat(), name), b"x")

    assert lake.latest_files("sale")[-1] == lake.list_table("sale")[-1]
    assert all("dt=2026-08-31" in k for k in lake.latest_files("sale"))
    assert lake.latest_files("khong_co") == []
