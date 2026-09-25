"""Diễn tập cảnh báo — điều kiện cổng B "mọi ngưỡng cảnh báo đã cấu hình và ĐÃ KÍCH HOẠT THỬ".

Mỗi diễn tập TẠO ĐIỀU KIỆN THẬT cho một (nhóm) rule — làm chậm DB, dừng worker, giữ transaction
mở, xóa partition tương lai, làm hỏng một số dư... — rồi chờ Grafana đưa rule sang `firing` VÀ
`alert-sink` nhận thông báo, rồi khôi phục. Không hạ ngưỡng, không sửa rule để nó kêu.

Rule không có diễn tập ở đây được kích hoạt ở chỗ khác: `rs-sync-failure-rate`, `rs-circuit-open`
(CH-1), `rs-store-disk` (CH-4), `rs-central-pg-conn` (LD-4), `rs-loaded-until`, `rs-transform`,
`rs-maintenance` (trong đợt đo khối lượng dài, khi Airflow/bảo trì dừng nhiều giờ). Tổng kết:
`uv run python infra/alert_watch.py --report runs/alerts/timeline.jsonl`.

OPT-IN (`RETAIL_SYNC_SCENARIOS=1` + `RETAIL_SYNC_ALERT_DRILLS=1`, `make test-alert-drills`).
Cần `--profile observability`. `test_drill_store_offline_70min` dài hơn một giờ — chạy riêng.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from simulator.audit import CONVERGED, audit, audit_until_settled
from simulator.manifest import Manifest, SaleRecord
from simulator.telemetry import AuditPublisher
from tests.scenarios.conftest import ROOT, VIRTUAL_KEYS, Stack
from tests.scenarios.harness import (
    container_of,
    docker,
    edge_dsn,
    observability_up,
    report,
    run_edge_plans,
    sales_plan,
    wait_alert,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAIL_SYNC_ALERT_DRILLS") != "1" or not observability_up(),
    reason="diễn tập cảnh báo là opt-in: RETAIL_SYNC_ALERT_DRILLS=1 + --profile observability",
)

SINK = "http://localhost:8089/alerts"


def _notified(uid: str) -> bool:
    text = httpx.get(SINK, timeout=10).text
    return any(
        (row := json.loads(line)).get("rule_uid") == uid and row.get("status") == "firing"
        for line in text.splitlines()
    )


async def _expect(uid: str, *, within: float) -> dict[str, Any]:
    fired = await wait_alert(uid, within=within)
    await asyncio.sleep(20)  # group_wait 10 s của contact point
    notified = await asyncio.to_thread(_notified, uid)
    return {"uid": uid, "fired_after_seconds": fired, "notified": notified}


def _ops(*args: str, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["uv", "run", "python", *args],  # noqa: S607
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1", **env},
        capture_output=True,
        text=True,
        check=check,
        timeout=900,
    )


def _central_env(stack: Stack) -> dict[str, str]:
    return {"DATABASE_URL": stack.central_dsn.replace("postgresql://", "postgresql+asyncpg://")}


def _psql(stack: Stack, sql: str) -> str:
    e = stack.env
    return docker(
        "exec", container_of("central-db"), "psql", "-U", e.get("CENTRAL_DB_USER", "central_app"),
        "-d", "central", "-Atc", sql,
    )  # fmt: skip


def _assert_all(results: list[dict[str, Any]]) -> None:
    for r in results:
        assert r["fired_after_seconds"] is not None, f"{r['uid']} không kêu"
        assert r["notified"], f"{r['uid']} không tới alert-sink"


# ═══════════════════ Dữ liệu sai: INV-4, đối soát xuyên tầng, dead-letter ═══════════════════


async def test_drill_drift_is_found_by_the_full_scan(stack: Stack) -> None:
    """rs-drift + rs-reconcile-full: làm HỎNG số dư của một khách (không có giao dịch mới — đúng
    thứ lượt tăng dần bỏ qua). Lần quét toàn bộ phát hiện; sửa lại → lần sau đóng lệch."""
    results = []
    # Quét toàn bộ "đã 40 ngày chưa chạy" → rs-reconcile-full.
    _psql(
        stack,
        "INSERT INTO reconciliation_watermark (job_name, last_run_at)"
        " VALUES ('point_balance_vs_ledger:full', now() - interval '40 days')"
        " ON CONFLICT (job_name) DO UPDATE SET last_run_at = now() - interval '40 days'",
    )
    results.append(await _expect("rs-reconcile-full", within=300))

    customer = _psql(
        stack, "SELECT customer_id FROM point_balance ORDER BY updated_at DESC LIMIT 1"
    ).strip()
    assert customer
    _psql(stack, f"UPDATE point_balance SET balance = balance + 1 WHERE customer_id = '{customer}'")
    try:
        found = await asyncio.to_thread(
            _ops, "-m", "central.ops.reconcile", "--full", env=_central_env(stack), check=False
        )
        assert "DRIFT" in found.stdout, found.stdout[-500:]
        results.append(await _expect("rs-drift", within=300))
    finally:
        _psql(
            stack,
            f"UPDATE point_balance SET balance = balance - 1 WHERE customer_id = '{customer}'",
        )
    healed = await asyncio.to_thread(_ops, "-m", "central.ops.reconcile", env=_central_env(stack))
    resolved = await wait_alert("rs-drift", state="inactive", within=300)
    report("alerts", "drill-drift", {"results": results, "healed": healed.stdout[-300:],
                                      "drift_resolved_after_seconds": resolved})  # fmt: skip
    _assert_all(results)
    assert resolved is not None


async def test_drill_audit_diverged(stack: Stack) -> None:
    """rs-audit-diverged: đối soát một đáp án có ĐƠN MA (manifest bị sửa) → DIVERGED."""
    manifest = await run_edge_plans(
        stack, "drill-audit", {"store-001": sales_plan(duration=15, sales=3, seed=90)}
    )
    good = await audit_until_settled(
        manifest, edge_dsns={"store-001": edge_dsn(stack, "store-001")},
        central_dsn=stack.central_dsn, wait_seconds=300, poll_seconds=3,
    )  # fmt: skip
    assert good.status == CONVERGED
    ghost = Manifest.read(ROOT / "runs" / manifest.run_id / "manifest.json")
    record = ghost.stores["store-001"]
    fake = str(uuid.uuid4())
    shift = next(iter(record.shifts))
    record.sales[fake] = SaleRecord(fake, shift, next(iter(record.sales.values())).business_date,
                                    123_000, {"CASH": 123_000}, 12, None)  # fmt: skip
    bad = await audit(
        ghost, edge_dsns={"store-001": edge_dsn(stack, "store-001")}, central_dsn=stack.central_dsn
    )
    publisher = AuditPublisher("http://localhost:4318")
    current = [bad]

    async def keep_publishing() -> None:
        # Như `audit --watch`: đẩy lại mỗi 30 giây, series không bao giờ "cũ" trong lúc chờ.
        while True:
            publisher.publish(current[0])
            await asyncio.sleep(30)

    pusher = asyncio.create_task(keep_publishing())
    try:
        result = await _expect("rs-audit-diverged", within=300)
        current[0] = good
        resolved = await wait_alert("rs-audit-diverged", state="inactive", within=300)
    finally:
        pusher.cancel()
        await asyncio.gather(pusher, return_exceptions=True)
        publisher.close()
    report("alerts", "drill-audit", {"result": result, "bad_status": bad.status,
                                      "resolved_after_seconds": resolved})  # fmt: skip
    assert bad.status == "DIVERGED"
    _assert_all([result])


async def test_drill_dead_letter(stack: Stack) -> None:
    """rs-dead-letter: một sự kiện ĐỘC (`schema_version` 99) thật sự đi qua `POST /events`."""
    key = VIRTUAL_KEYS.read_text(encoding="utf-8").splitlines()
    store_id, api_key = next(line.split("=", 1) for line in key if "=" in line and line[0] != "#")
    event_id = str(uuid.uuid4())
    poison = {
        "event_id": event_id, "event_type": "SaleCompleted", "schema_version": 99,
        "store_id": store_id, "occurred_at": datetime.now(UTC).isoformat(), "payload": {},
    }  # fmt: skip
    r = await asyncio.to_thread(
        httpx.post,
        f"{stack.central_url}/api/v1/events",
        json=[poison],
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    assert r.status_code == 200 and r.json()["rejected"][0]["retryable"] is False
    try:
        result = await _expect("rs-dead-letter", within=300)
    finally:
        _psql(stack, f"DELETE FROM dead_letter_event WHERE event_id = '{event_id}'")
    resolved = await wait_alert("rs-dead-letter", state="inactive", within=300)
    report("alerts", "drill-dead-letter", {"result": result, "resolved_after_seconds": resolved})
    _assert_all([result])


# ═══════════════════ Lịch bảo trì, trích xuất, bộ giám sát ═══════════════════


async def test_drill_partition_buffer(stack: Stack) -> None:
    """rs-partition: vùng đệm partition cạn (2 tháng xa nhất biến mất, như lịch ngừng 2 tháng)
    → cảnh báo; một lượt `central.ops.maintenance` → vùng đệm về đủ, hết cảnh báo."""
    rows = _psql(
        stack,
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
        " WHERE i.inhparent = 'point_ledger'::regclass ORDER BY 1 DESC LIMIT 2",
    ).split()
    for name in rows:
        assert _psql(stack, f"SELECT count(*) FROM {name}").strip() == "0", name  # chỉ tháng rỗng
    for name in rows:
        _psql(stack, f"DROP TABLE {name}")
    result = await _expect("rs-partition", within=300)
    healed = await asyncio.to_thread(_ops, "-m", "central.ops.maintenance", env=_central_env(stack))
    resolved = await wait_alert("rs-partition", state="inactive", within=300)
    report("alerts", "drill-partition", {"dropped": rows, "result": result,
                                          "maintenance": healed.stdout[-300:],
                                          "resolved_after_seconds": resolved})  # fmt: skip
    _assert_all([result])
    assert resolved is not None


async def test_drill_long_transaction_holds_the_extract_horizon(stack: Stack) -> None:
    """rs-horizon: một phiên giữ transaction mở 12 phút → `extract_horizon()` đứng lại (bẫy 1:
    an toàn, không mất dữ liệu, nhưng luồng ngừng tiến) → cảnh báo sau 5 + 5 phút."""
    e = stack.env
    holder = subprocess.Popen(  # noqa: S603, ASYNC220
        ["docker", "exec", container_of("central-db"), "psql", "-U",  # noqa: S607
         e.get("CENTRAL_DB_USER", "central_app"), "-d", "central", "-c",
         "BEGIN; SELECT txid_current(); SELECT pg_sleep(900); COMMIT;"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        result = await _expect("rs-horizon", within=900)
    finally:
        _psql(stack, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                     " WHERE query LIKE '%pg_sleep(900)%' AND pid <> pg_backend_pid()")  # fmt: skip
        holder.kill()
    resolved = await wait_alert("rs-horizon", state="inactive", within=600)
    report("alerts", "drill-horizon", {"result": result, "resolved_after_seconds": resolved})
    _assert_all([result])


async def test_drill_monitor_loses_the_lake(stack: Stack) -> None:
    """rs-monitor: MinIO chết 7 phút → bộ giám sát báo mất nguồn `lake`. Vận hành không ảnh hưởng
    (docs/03 §5); chỉ pipeline lỡ lượt đó."""
    minio = container_of("minio")
    docker("stop", minio)
    try:
        result = await _expect("rs-monitor", within=720)
    finally:
        docker("start", minio)
    resolved = await wait_alert("rs-monitor", state="inactive", within=600)
    report("alerts", "drill-monitor", {"result": result, "resolved_after_seconds": resolved})
    _assert_all([result])


# ═══════════════ Chậm: POST /sales, pool cửa hàng, POST /events, trễ đồng bộ ═══════════════


async def test_drill_slow_store_database(stack: Stack) -> None:
    """rs-sales-p95 + rs-db-pool: Postgres của store-002 bị bóp còn 5% một CPU trong khi quầy vẫn
    bán dồn dập → chốt đơn p95 > 500 ms suốt 10 phút, pool kết nối > 80% suốt 5 phút."""
    store = "store-002"
    db = container_of(f"edge-db-{store}")
    docker("update", "--cpus", "0.05", db)
    try:
        run = asyncio.create_task(
            run_edge_plans(
                stack,
                "drill-slow-store",
                {store: sales_plan(duration=16 * 60, sales=16 * 60 * 4, seed=91, tail=30)},
                request_timeout=60,
            )
        )
        results = [
            await _expect("rs-db-pool", within=900),
            await _expect("rs-sales-p95", within=900),
        ]
    finally:
        docker("update", "--cpus", "0", db)
    manifest = await run
    summary = {
        "results": results,
        "sales": len(manifest.stores[store].sales),
        "latency_ms": manifest.stats()["latency_ms"],
    }
    report("alerts", "drill-slow-store", summary)
    _assert_all(results)


async def test_drill_slow_central_database(stack: Stack) -> None:
    """rs-ingest-p95 + rs-store-lag: Postgres trung tâm bị bóp còn 10% CPU dưới tải của 5 cửa
    hàng ảo đẩy lịch sử (occurred_at vài ngày trước → trễ đồng bộ > 15 phút là THẬT)."""
    from simulator.__main__ import main as simulator

    keys = ROOT / "runs" / "virtual-keys.env"
    db = container_of("central-db")
    docker("update", "--cpus", "0.1", db)
    try:
        run = asyncio.create_task(
            asyncio.to_thread(
                simulator,
                ["run", "--mode", "virtual", "--profile", "t2", "--keys", str(keys),
                 "--stores", "5", "--days", "2", "--rate", "90", "--seed", "92",
                 "--central", stack.central_url, "--drain-timeout", "1800"],
            )
        )  # fmt: skip
        results = [
            await _expect("rs-store-lag", within=1200),
            await _expect("rs-ingest-p95", within=1200),
        ]
    finally:
        docker("update", "--cpus", "0", db)
    code = await run
    report("alerts", "drill-slow-central", {"results": results, "simulator_exit": code})
    _assert_all(results)


# ═══════════════════ Cửa hàng im lặng 70 phút ═══════════════════


async def test_drill_store_offline_70min(stack: Stack) -> None:
    """rs-outbox-depth + rs-outbox-oldest + rs-store-silent: sync worker của store-003 CHẾT (không
    phải mất mạng: không còn heartbeat) trong khi quầy vẫn bán dồn > 5000 sự kiện. Kêu đủ ba thì
    bật worker lại: toàn bộ tồn đọng phải lên trung tâm trong 5 phút (NFR-04)."""
    store = "store-003"
    worker = container_of(f"edge-sync-worker-{store}")
    docker("stop", worker)
    started = datetime.now(UTC)
    try:
        manifest = await run_edge_plans(
            stack,
            "drill-offline",
            {store: sales_plan(duration=16 * 60, sales=2800, seed=93, tail=20, new_share=0.35)},
        )
        results = [await _expect("rs-outbox-depth", within=900)]
        results.append(await _expect("rs-store-silent", within=4800))
        results.append(await _expect("rs-outbox-oldest", within=2400))
    finally:
        docker("start", worker)
    offline_minutes = (datetime.now(UTC) - started) / timedelta(minutes=1)
    t0 = datetime.now(UTC)
    settled = await audit_until_settled(
        manifest, edge_dsns={store: edge_dsn(stack, store)}, central_dsn=stack.central_dsn,
        wait_seconds=300, poll_seconds=5,
    )  # fmt: skip
    drain = (datetime.now(UTC) - t0).total_seconds()
    conn = await asyncpg.connect(edge_dsn(stack, store))
    try:
        backlog = await conn.fetchval("SELECT count(*) FROM outbox WHERE created_at >= $1", started)
    finally:
        await conn.close()
    report("alerts", "drill-store-offline", {
        "results": results, "offline_minutes": round(offline_minutes, 1),
        "backlog_events": backlog, "drain_seconds": round(drain, 1),
        "final_status": settled.status, "manifest": manifest.run_id,
    })  # fmt: skip
    _assert_all(results)
    assert settled.status == CONVERGED, settled.findings[:5]
