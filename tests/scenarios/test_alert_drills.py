"""Diễn tập cảnh báo — điều kiện cổng B "mọi ngưỡng cảnh báo đã cấu hình và ĐÃ KÍCH HOẠT THỬ".

Mỗi diễn tập TẠO ĐIỀU KIỆN THẬT cho một (nhóm) rule — làm chậm DB, dừng worker, giữ transaction
mở, xóa partition tương lai, làm hỏng một số dư... — rồi chờ Grafana đưa rule sang `firing` VÀ
`alert-sink` nhận thông báo, rồi khôi phục. Không hạ ngưỡng, không sửa rule để nó kêu.

Rule không có diễn tập ở đây được kích hoạt ở chỗ khác: `rs-sync-failure-rate`, `rs-circuit-open`
(CH-1), `rs-store-disk` (CH-4), `rs-central-pg-conn` (LD-4). Tổng kết:
`uv run python infra/alert_watch.py --report runs/alerts/timeline.jsonl`.

OPT-IN (`RETAIL_SYNC_SCENARIOS=1` + `RETAIL_SYNC_ALERT_DRILLS=1`, `make test-alert-drills`).
Cần `--profile observability`. Ba diễn tập DÀI chạy riêng (job `proof` trên GitHub Actions chạy
mỗi cái trên một máy): `store_offline_70min` (~1,5 giờ), `transform_stalls` (≤ 2,5 giờ),
`pipeline_and_maintenance_stop` (~3,2 giờ) — ngưỡng tính bằng giờ, và KHÔNG hạ ngưỡng để thử.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
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
    airflow,
    container_of,
    docker,
    edge_dsn,
    observability_up,
    pipeline_env,
    prom,
    report,
    run_dag,
    run_edge_plans,
    sales_plan,
    wait_alert,
)
from tests.scenarios.harness import notified as notified_by_sink

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAIL_SYNC_ALERT_DRILLS") != "1" or not observability_up(),
    reason="diễn tập cảnh báo là opt-in: RETAIL_SYNC_ALERT_DRILLS=1 + --profile observability",
)


async def _expect(uid: str, *, within: float) -> dict[str, Any]:
    fired = await wait_alert(uid, within=within)
    await asyncio.sleep(20)  # group_wait 10 s của contact point
    notified = await asyncio.to_thread(notified_by_sink, uid)
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


def _unthrottle(name: str) -> None:
    """Gỡ giới hạn CPU. `docker update --cpus 0` KHÔNG gỡ (giữ nguyên giới hạn cũ — lần diễn tập
    đầu để DB store-002 bị bóp 5% CPU sau khi xong): đặt bằng đúng số CPU của máy ảo Docker."""
    ncpu = docker("info", "--format", "{{.NCPU}}").strip() or "8"
    docker("update", "--cpus", ncpu, name)


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

    latest = "SELECT customer_id FROM point_balance ORDER BY updated_at DESC LIMIT 1"
    if not _psql(stack, latest).strip():
        # Stack sạch (CI): chưa khách nào có điểm. Bán vài đơn cho khách mới qua Edge API thật.
        seeded = await run_edge_plans(
            stack, "drill-drift-seed", {"store-001": sales_plan(duration=20, sales=4, seed=89,
                                                                new_share=1.0)}
        )  # fmt: skip
        settled = await audit_until_settled(
            seeded, edge_dsns={"store-001": edge_dsn(stack, "store-001")},
            central_dsn=stack.central_dsn, wait_seconds=300, poll_seconds=3,
        )  # fmt: skip
        assert settled.status == CONVERGED
    customer = _psql(stack, latest).strip()
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


def _outbox_lock_sql(seconds: float) -> str:
    """Giữ khóa EXCLUSIVE trên `outbox` của cửa hàng: đọc vẫn qua, mọi GHI phải chờ. Mọi lần chốt
    đơn và đăng ký khách đều ghi outbox trong cùng transaction (ADR-003) → chúng chờ khóa trong
    lúc GIỮ kết nối pool. Giống một truy vấn báo cáo/bảo trì dài khóa bảng giữa giờ bán."""
    return f"BEGIN; LOCK TABLE outbox IN EXCLUSIVE MODE; SELECT pg_sleep({seconds}); COMMIT;"


def _edge_psql(stack: Stack, store: str, sql: str) -> list[str]:
    e = stack.env
    db = "edge_" + store.replace("-", "_")
    return ["docker", "exec", container_of(f"edge-db-{store}"), "psql", "-U",
            e.get("EDGE_DB_USER", "edge_app"), "-d", db, "-Atc", sql]  # fmt: skip


def _release_outbox_lock(stack: Stack, store: str) -> None:
    subprocess.run(  # noqa: S603
        _edge_psql(stack, store, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                   " WHERE query LIKE '%LOCK TABLE outbox%' AND pid <> pg_backend_pid()"),
        capture_output=True, timeout=60, check=False,
    )  # fmt: skip


# Bản đầu của hai diễn tập dưới bóp CPU Postgres cửa hàng (`docker update --cpus`). Kết quả phụ
# thuộc tốc độ máy: 5% CPU thì runner CI theo kịp (p95 lượn quanh 500 ms), 2% thì Postgres không mở
# nổi kết nối overflow mới → pool kẹt ~10/20 = 50% và metric edge-api gần như không có điểm đo
# (workflow `proof` 2026-09-28 và 2026-10-07: không rule nào kêu trong 30 phút bóp). Khóa bảng
# cho cùng hiệu ứng "DB chậm với ghi" trên MỌI máy.


async def test_drill_store_pool_exhausted(stack: Stack) -> None:
    """rs-db-pool: `outbox` của store-002 bị khóa liên tục trong khi quầy bán 4 đơn/s → mỗi lần
    chốt giữ một kết nối chờ khóa, pool (10 + 10 overflow) đầy → > 80% suốt 5 phút."""
    store = "store-002"
    run = asyncio.create_task(
        run_edge_plans(
            stack, "drill-pool", {store: sales_plan(duration=14 * 60, sales=14 * 60 * 4, seed=91,
                                                     start=20, tail=30)},
            request_timeout=60,
        )
    )  # fmt: skip
    await asyncio.sleep(15)  # đăng nhập + mở ca trước khi khóa
    holder = subprocess.Popen(  # noqa: S603, ASYNC220
        _edge_psql(stack, store, _outbox_lock_sql(720)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        result = await _expect("rs-db-pool", within=720)
    finally:
        _release_outbox_lock(stack, store)
        holder.kill()
    manifest = await run
    resolved = await wait_alert("rs-db-pool", state="inactive", within=600)
    report("alerts", "drill-store-pool", {
        "result": result, "resolved_after_seconds": resolved,
        "sales": len(manifest.stores[store].sales), "unknown": len(manifest.stores[store].unknown),
        "latency_ms": manifest.stats()["latency_ms"],
    })  # fmt: skip
    _assert_all([result])


async def test_drill_slow_store_database(stack: Stack) -> None:
    """rs-sales-p95: `outbox` của store-002 bị khóa từng nhịp (2 s khóa, 1 s nhả) trong khi quầy bán
    4 đơn/s → mọi lần chốt chờ tới ~2 s, vẫn hoàn tất đều đặn → p95 `POST /sales` > 500 ms suốt
    10 phút. Nhịp ngắn để pool KHÔNG đầy: đây là "chậm", không phải "nghẽn"."""
    store = "store-002"
    run = asyncio.create_task(
        run_edge_plans(
            stack, "drill-slow-store", {store: sales_plan(duration=17 * 60, sales=17 * 60 * 4,
                                                           seed=92, start=20, tail=30)},
            request_timeout=60,
        )
    )  # fmt: skip
    await asyncio.sleep(15)
    stop = asyncio.Event()

    async def pulse() -> None:
        while not stop.is_set():
            await asyncio.to_thread(
                subprocess.run, _edge_psql(stack, store, _outbox_lock_sql(2)),
                capture_output=True, timeout=60, check=False,
            )  # fmt: skip
            await asyncio.sleep(1)

    pulser = asyncio.create_task(pulse())
    try:
        result = await _expect("rs-sales-p95", within=900)
    finally:
        stop.set()
        await pulser
        _release_outbox_lock(stack, store)
    manifest = await run
    report("alerts", "drill-slow-store", {
        "result": result, "sales": len(manifest.stores[store].sales),
        "latency_ms": manifest.stats()["latency_ms"],
    })  # fmt: skip
    _assert_all([result])


async def test_drill_slow_central_database(stack: Stack) -> None:
    """rs-ingest-p95 + rs-store-lag: Postgres trung tâm bị bóp còn 5% CPU dưới tải của 20 cửa
    hàng ảo đẩy lịch sử (occurred_at vài ngày trước → trễ đồng bộ > 15 phút là THẬT).

    Bản đầu (10% CPU, 5 cửa hàng) chỉ kêu được `rs-store-lag` trên runner CI: 5 cửa hàng gửi tuần
    tự không bao giờ giữ đủ lô đồng thời để p95 vượt 1 s (workflow `proof` 2026-09-28)."""
    from simulator.__main__ import main as simulator

    keys = ROOT / "runs" / "virtual-keys.env"
    db = container_of("central-db")
    docker("update", "--cpus", "0.05", db)
    try:
        run = asyncio.create_task(
            asyncio.to_thread(
                simulator,
                ["run", "--mode", "virtual", "--profile", "t2", "--keys", str(keys),
                 "--stores", "20", "--days", "2", "--rate", "90", "--seed", "92",
                 "--central", stack.central_url, "--drain-timeout", "1800"],
            )
        )  # fmt: skip
        results = [
            await _expect("rs-store-lag", within=1200),
            await _expect("rs-ingest-p95", within=1200),
        ]
    finally:
        _unthrottle(db)
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


# ═══════════════ Đường ống đứng nhiều giờ: DAG tạm dừng, bảo trì chết ═══════════════


async def test_drill_transform_stalls(stack: Stack) -> None:
    """rs-transform: S4/S5 vẫn chạy (`pipeline run` từ ngoài, như `make pipeline-run`) nhưng DAG,
    nơi DUY NHẤT chạy dbt, tạm dừng → đơn đã nạp mà mart chưa có già dần; > 70 phút thì kêu.

    Cửa sổ trích đóng theo giờ, nên đơn chỉ thành "đã nạp" sau mốc giờ kế tiếp: vòng lặp chạy
    `pipeline run` mỗi 5 phút tới khi bộ giám sát thấy đơn chờ dbt, rồi mới chờ cảnh báo."""
    assert await asyncio.to_thread(run_dag) == "success"  # mốc: mart bắt kịp bronze
    await asyncio.to_thread(airflow, "dags", "pause", "retail_pipeline")
    loads = 0
    try:
        manifest = await run_edge_plans(
            stack, "drill-transform", {"store-001": sales_plan(duration=30, sales=5, seed=94)}
        )
        pending = 0.0
        deadline = time.monotonic() + 80 * 60
        while time.monotonic() < deadline:
            await asyncio.to_thread(_ops, "-m", "pipeline", "run", env=pipeline_env(stack))
            loads += 1
            await asyncio.sleep(90)  # ba chu kỳ của flow-monitor
            if (pending := prom("max(pipeline_transform_pending_sales)") or 0.0) > 0:
                break
            await asyncio.sleep(210)
        assert pending > 0, "80 phút mà bronze chưa có đơn nào chờ dbt"
        result = await _expect("rs-transform", within=80 * 60)
    finally:
        await asyncio.to_thread(airflow, "dags", "unpause", "retail_pipeline")
    assert await asyncio.to_thread(run_dag) == "success"
    resolved = await wait_alert("rs-transform", state="inactive", within=600)
    report("alerts", "drill-transform", {
        "result": result, "host_pipeline_runs": loads, "pending_sales_seen": pending,
        "manifest": manifest.run_id, "resolved_after_seconds": resolved,
    })  # fmt: skip
    _assert_all([result])
    assert resolved is not None


async def test_drill_pipeline_and_maintenance_stop(stack: Stack) -> None:
    """rs-loaded-until + rs-maintenance: DAG tạm dừng VÀ `central-maintenance` chết — không ai chạy
    S4/S5, không ai đối soát INV-4 hay tạo partition. Mép "đã nạp tới" và lượt đối soát cuối già
    quá 3 giờ; cả hai rule `for: 0s` nên kêu ngay khi vượt (~3 giờ 5 phút tính từ lúc dừng)."""
    maintenance = container_of("central-maintenance")
    assert await asyncio.to_thread(run_dag) == "success"  # đủ 7 bảng có mép "đã nạp tới"
    assert _psql(
        stack,
        "SELECT count(*) FROM reconciliation_watermark WHERE job_name = 'point_balance_vs_ledger'",
    ).strip() == "1", "central-maintenance chưa chạy lượt nào — không có gì để già đi"  # fmt: skip
    await asyncio.to_thread(airflow, "dags", "pause", "retail_pipeline")
    docker("stop", maintenance)
    deadline = time.monotonic() + 3 * 3600 + 20 * 60
    try:
        results = [
            await _expect(uid, within=max(60.0, deadline - time.monotonic()))
            for uid in ("rs-maintenance", "rs-loaded-until")
        ]
    finally:
        docker("start", maintenance)
        await asyncio.to_thread(airflow, "dags", "unpause", "retail_pipeline")
    assert await asyncio.to_thread(run_dag) == "success"
    resolved = {
        uid: await wait_alert(uid, state="inactive", within=600)
        for uid in ("rs-maintenance", "rs-loaded-until")
    }
    report("alerts", "drill-pipeline-stop", {"results": results, "resolved": resolved})
    _assert_all(results)
    assert all(v is not None for v in resolved.values()), resolved
