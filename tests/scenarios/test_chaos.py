"""CH-1…CH-7 trên compose THẬT, nguồn tải là bộ giả lập (docs/08 §6.1, docs/16 §3).

Điều kiện đạt chung: sau mỗi kịch bản `simulator audit` về `CONVERGED` (L0 → L1 → L2) trong
thời hạn hội tụ — cộng tiêu chí riêng của từng kịch bản. Mỗi kịch bản ghi bằng chứng vào
`runs/chaos/<phiên>/CH-n.json`.

**OPT-IN kép** (`RETAIL_SYNC_SCENARIOS=1` + `RETAIL_SYNC_CHAOS=1`, `make test-chaos`): các test
này GIẾT tiến trình, ngắt mạng, làm đầy đĩa trên stack đang chạy, và CH-1 mặc định kéo dài 30
phút. Cần `--profile edge --profile edge-multi --profile central --profile observability`.

Thời hạn hội tụ 300 s = NFR-04 ("< 5 phút sau khi nối lại") — trần backoff của worker là 240 s
(`SyncSettings.backoff_max_seconds`), nên lần thử lại cuối rơi vào trong thời hạn.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import time
import uuid
from typing import Any

import asyncpg
import httpx
import pytest

from edge.sync.client import HttpCentralClient
from simulator.audit import CONVERGED, CONVERGING, audit, audit_until_settled
from simulator.manifest import Manifest
from simulator.profile import load_profile
from simulator.quirks import Quirks
from simulator.sinks.virtual import read_keys, run_virtual
from tests.scenarios.conftest import ENV_FILE, ROOT, VIRTUAL_KEYS, Stack
from tests.scenarios.harness import (
    EDGE,
    NETWORK,
    Probe,
    container_of,
    docker,
    edge_dsn,
    env_minutes,
    is_up,
    observability_up,
    prom,
    report,
    run_edge_plans,
    sales_plan,
    stores_up,
    wait_alert,
    wait_up,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAIL_SYNC_CHAOS") != "1",
    reason="test hỗn loạn là opt-in: RETAIL_SYNC_CHAOS=1 (make test-chaos)",
)

CONVERGE_SECONDS = 300.0

#: Không có "đơn nửa vời": mọi đơn của các ca trong lần chạy có dòng hàng, Σ thanh toán = tổng
#: tiền (DI-3) và Σ dòng − chiết khấu = tổng tiền (DI-2). Ràng buộc DB đã cấm, test vẫn đếm.
_HALF_SALES = """
SELECT s.sale_id FROM sale s
LEFT JOIN (SELECT sale_id, sum(amount) AS paid FROM sale_payment GROUP BY sale_id) p
       ON p.sale_id = s.sale_id
LEFT JOIN (SELECT sale_id, sum(line_total) AS lines, count(*) AS n FROM sale_line
           GROUP BY sale_id) l ON l.sale_id = s.sale_id
WHERE s.shift_id = ANY($1::uuid[])
  AND (l.n IS NULL OR p.paid IS DISTINCT FROM s.total
       OR l.lines - s.discount_tier - s.discount_promo IS DISTINCT FROM s.total)
"""


def _logs_stderr(name: str) -> str:
    """Postgres ghi log ra stderr — `docker logs` trả nó ở stderr của lệnh."""
    r = subprocess.run(  # noqa: S603
        ["docker", "logs", "--since", "15m", name],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stderr


def _get(url: str) -> httpx.Response:
    return httpx.get(url, timeout=10)


def _shift_ids(manifest: Manifest, store_id: str) -> list[uuid.UUID]:
    return [uuid.UUID(s) for s in manifest.stores[store_id].shifts]


async def _half_sales(stack: Stack, manifest: Manifest, store_id: str) -> int:
    conn = await asyncpg.connect(edge_dsn(stack, store_id))
    try:
        return len(await conn.fetch(_HALF_SALES, _shift_ids(manifest, store_id)))
    finally:
        await conn.close()


async def _outbox_attempts(stack: Stack, store_id: str) -> tuple[int, int]:
    conn = await asyncpg.connect(edge_dsn(stack, store_id))
    try:
        row = await conn.fetchrow(
            "SELECT count(*) AS pending, COALESCE(max(attempts), 0) AS attempts FROM outbox"
            " WHERE sent_at IS NULL AND dead_lettered_at IS NULL"
        )
        return int(row["pending"]), int(row["attempts"])
    finally:
        await conn.close()


def _dsns(stack: Stack, stores: list[str]) -> dict[str, str]:
    return {sid: edge_dsn(stack, sid) for sid in stores}


def _summary(manifest: Manifest) -> dict[str, Any]:
    return {
        sid: {
            "sales_201": len(r.sales),
            "unknown": len(r.unknown),
            "rejected": len(r.rejected),
            "shifts": len(r.shifts),
        }
        for sid, r in manifest.stores.items()
    } | {"latency_ms": manifest.stats()["latency_ms"]}


# ═══════════════════════════════ CH-1 ═══════════════════════════════


async def test_ch1_network_disconnect_30min(stack: Stack) -> None:
    """`docker network disconnect` trung tâm khi ĐANG BÁN ở cả 3 cửa hàng. Đạt: 0 đơn mất (cửa
    hàng bán được suốt, `attempts` = 0), hội tụ < 5 phút sau khi nối lại."""
    minutes = env_minutes("CHAOS_CH1_MINUTES", 30)
    stores = stores_up()
    duration = minutes * 60
    plans = {
        sid: sales_plan(duration=duration, sales=int(minutes * 3), seed=100 + i)
        for i, sid in enumerate(stores)
    }
    central = container_of("central-api")
    docker("network", "disconnect", NETWORK, central)
    disconnected_at = time.monotonic()
    reconnected = False
    try:
        run = asyncio.create_task(run_edge_plans(stack, "ch1", plans))
        await asyncio.sleep(duration / 2)
        mid: dict[str, Any] = {}
        for sid in stores:
            mid[sid] = dict(
                zip(("pending", "max_attempts"), await _outbox_attempts(stack, sid), strict=True)
            )
        manifest = await run
        during = await audit(
            manifest, edge_dsns=_dsns(stack, stores), central_dsn=stack.central_dsn
        )
        after_sales = {sid: await _outbox_attempts(stack, sid) for sid in stores}
    finally:
        docker("network", "connect", "--alias", "central-api", NETWORK, central)
        reconnected = True
    offline_seconds = time.monotonic() - disconnected_at
    t0 = time.monotonic()
    settled = await audit_until_settled(
        manifest,
        edge_dsns=_dsns(stack, stores),
        central_dsn=stack.central_dsn,
        wait_seconds=CONVERGE_SECONDS,
        poll_seconds=5,
    )
    converge = time.monotonic() - t0
    report(
        "chaos",
        "CH-1",
        {
            "stores": stores,
            "offline_seconds": round(offline_seconds),
            "mid_outbox": mid,
            "after_sales_outbox": {
                s: {"pending": p, "max_attempts": a} for s, (p, a) in after_sales.items()
            },
            "during_status": during.status,
            "converge_seconds_after_reconnect": round(converge, 1),
            "final_status": settled.status,
            "findings": [
                f.__dict__ if hasattr(f, "__dict__") else str(f) for f in settled.findings[:10]
            ],
            "manifest": manifest.run_id,
            "summary": _summary(manifest),
        },
    )
    assert reconnected
    for sid in stores:
        assert len(manifest.stores[sid].sales) > 0, f"{sid} không bán được khi mất trung tâm"
        assert manifest.stores[sid].unknown == []
        assert after_sales[sid][1] == 0, "lỗi đường truyền đã đốt lượt thử (AT-02)"
    assert during.status == CONVERGING, during.findings[:5]  # chưa tới, không phải mất
    assert settled.status == CONVERGED, settled.findings[:5]
    assert converge < CONVERGE_SECONDS


# ═══════════════════════════════ CH-2 ═══════════════════════════════


async def test_ch2_kill_app_mid_commit_x20(stack: Stack) -> None:
    """`kill -9` Edge API giữa lúc chốt đơn, lặp 20 lần. Đạt: không có đơn nửa vời, Σ thanh toán
    = tổng tiền luôn đúng, đơn "không rõ kết cục" có ở mọi tầng hoặc không tầng nào."""
    store = "store-001"
    url = EDGE[store][0]
    duration = 240.0
    plan = sales_plan(duration=duration, sales=600, seed=2, tail=20)  # ~2,5 đơn/giây
    api = container_of(f"edge-api-{store}")
    kills: list[dict[str, float]] = []

    async def killer() -> None:
        rng = random.Random(22)
        await asyncio.sleep(8)
        for _ in range(20):
            await asyncio.sleep(rng.uniform(2.0, 5.0))
            t = time.monotonic()
            await asyncio.to_thread(docker, "kill", "-s", "KILL", api)
            await asyncio.to_thread(docker, "start", api)
            back = await wait_up(url, within=120)
            kills.append({"restart_seconds": round(time.monotonic() - t, 1), "health_wait": back})

    run = asyncio.create_task(run_edge_plans(stack, "ch2", {store: plan}, request_timeout=10))
    await killer()
    manifest = await run
    settled = await audit_until_settled(
        manifest,
        edge_dsns=_dsns(stack, [store]),
        central_dsn=stack.central_dsn,
        wait_seconds=CONVERGE_SECONDS,
        poll_seconds=5,
    )
    half = await _half_sales(stack, manifest, store)
    report(
        "chaos",
        "CH-2",
        {
            "kills": kills,
            "half_sales": half,
            "final_status": settled.status,
            "findings": [str(f) for f in settled.findings[:10]],
            "manifest": manifest.run_id,
            "summary": _summary(manifest),
        },
    )
    assert len(kills) == 20
    assert half == 0
    assert settled.status == CONVERGED, settled.findings[:5]


# ═══════════════════════════════ CH-3 ═══════════════════════════════


async def test_ch3_kill_postgres_edge(stack: Stack) -> None:
    """`kill -9` Postgres cửa hàng (mất điện) 3 lần giữa giờ bán. Đạt: sau khởi động lại, 0 giao
    dịch đã commit bị mất — mọi đơn đã trả `201` có ở L1 (và hội tụ lên L2)."""
    store = "store-001"
    db = container_of(f"edge-db-{store}")
    duration = 180.0
    plan = sales_plan(duration=duration, sales=300, seed=3, tail=20)
    crashes: list[dict[str, Any]] = []

    async def crash_loop() -> None:
        for at in (30.0, 75.0, 120.0):
            await asyncio.sleep(max(0.0, at - (time.monotonic() - started)))
            t = time.monotonic()
            await asyncio.to_thread(docker, "kill", "-s", "KILL", db)
            await asyncio.to_thread(docker, "start", db)
            for _ in range(120):
                ready = await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "exec", db, "pg_isready", "-q"],
                    capture_output=True,
                    check=False,
                )
                if ready.returncode == 0:
                    break
                await asyncio.sleep(0.5)
            crashes.append({"at": at, "recovery_seconds": round(time.monotonic() - t, 1)})

    started = time.monotonic()
    run = asyncio.create_task(run_edge_plans(stack, "ch3", {store: plan}, request_timeout=10))
    await crash_loop()
    manifest = await run
    recovered = await asyncio.to_thread(docker, "logs", "--since", "15m", db, check=False)
    stderr_logs = await asyncio.to_thread(_logs_stderr, db)
    recovery_lines = [
        line
        for line in (recovered + stderr_logs).splitlines()
        if "not properly shut down" in line or "redo done" in line
    ]
    settled = await audit_until_settled(
        manifest,
        edge_dsns=_dsns(stack, [store]),
        central_dsn=stack.central_dsn,
        wait_seconds=CONVERGE_SECONDS,
        poll_seconds=5,
    )
    report(
        "chaos",
        "CH-3",
        {
            "crashes": crashes,
            "wal_recovery_log": recovery_lines[:12],
            "half_sales": await _half_sales(stack, manifest, store),
            "final_status": settled.status,
            "findings": [str(f) for f in settled.findings[:10]],
            "manifest": manifest.run_id,
            "summary": _summary(manifest),
        },
    )
    assert len(crashes) == 3
    assert any("not properly shut down" in line for line in recovery_lines)  # đúng là mất điện
    # L0 → L1 thiếu đơn nào là DIVERGED: không có đơn 201 nào mất sau 3 lần mất điện.
    assert settled.status == CONVERGED, settled.findings[:5]


# ═══════════════════════════════ CH-4 ═══════════════════════════════

_DISK = "store-disk"
_DISK_URL = "http://localhost:8009"


def _compose_disk(*args: str) -> None:
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker", "compose", "-p", "retail-sync",
            "-f", str(ROOT / "infra" / "compose.yaml"),
            "-f", str(ROOT / "infra" / "chaos" / "disk.compose.yaml"),
            "--env-file", str(ENV_FILE), "--profile", "chaos-disk", *args,
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )  # fmt: skip


def _ops(*args: str, env: dict[str, str]) -> None:
    subprocess.run(  # noqa: S603
        ["uv", "run", "python", *args],  # noqa: S607
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1", **env},
        check=True,
        capture_output=True,
        timeout=600,
    )


def _disk_usage(db: str) -> tuple[int, int]:
    out = docker("exec", db, "df", "-B1", "--output=size,used", "/var/lib/postgresql")
    size, used = out.strip().splitlines()[-1].split()
    return int(size), int(used)


async def test_ch4_disk_95_percent(stack: Stack) -> None:
    """Làm đầy đĩa của một cửa hàng tới 95%. Đạt: cảnh báo `rs-store-disk` (> 75%) PHÁT RA — và
    tới được `alert-sink` — trong khi cửa hàng VẪN bán được (chưa lần ghi nào lỗi)."""
    if not observability_up():
        pytest.skip("cần --profile observability (Grafana + alert-sink)")
    e = stack.env
    url = (
        f"postgresql+asyncpg://{e.get('EDGE_DB_USER', 'edge_app')}:{e['EDGE_DB_PASSWORD']}"
        "@localhost:5439/edge_store_disk"
    )
    db = container_of("edge-db-disk")
    _compose_disk("up", "-d", "--build", "--wait", "edge-db-disk", "edge-api-disk")
    try:
        await asyncio.to_thread(
            _ops, "-m", "alembic", "-c", "packages/edge/alembic.ini", "upgrade", "head",
            env={"DATABASE_URL": url},
        )  # fmt: skip
        await asyncio.to_thread(
            _ops, "-m", "edge.ops.seed", "--crawl-data", str(ROOT / "tests/fixtures/crawl_data")
            if not (ROOT / "crawl_data/products/product_details.csv").exists()
            else str(ROOT / "crawl_data"), "--employees",
            env={"DATABASE_URL": url, "STORE_ID": _DISK, "SEED_EMPLOYEE_PASSWORD": stack.password},
        )  # fmt: skip
        await wait_up(_DISK_URL, within=120)
        EDGE[_DISK] = (_DISK_URL, 5439, "edge_store_disk")

        size, used = _disk_usage(db)
        ballast = int(size * 0.95) - used
        docker(
            "exec", "-u", "root", db, "fallocate", "-l", str(ballast), "/var/lib/postgresql/ballast"
        )
        size, used = _disk_usage(db)
        filled_at = time.monotonic()
        fired_after = await wait_alert("rs-store-disk", within=420)

        # 95% đầy, cảnh báo đã kêu: cửa hàng vẫn phải bán được.
        manifest = await run_edge_plans(
            stack, "ch4", {_DISK: sales_plan(duration=40, sales=30, seed=4)}
        )
        sink = (await asyncio.to_thread(_get, "http://localhost:8089/alerts")).text
        notified = any(
            json.loads(line).get("rule_uid") == "rs-store-disk"
            and json.loads(line).get("status") == "firing"
            for line in sink.splitlines()
        )
        docker("exec", "-u", "root", db, "rm", "-f", "/var/lib/postgresql/ballast")
        resolved_after = await wait_alert("rs-store-disk", state="inactive", within=420)
        report(
            "chaos",
            "CH-4",
            {
                "disk_bytes": size,
                "used_ratio_after_fill": round(used / size, 4),
                "alert_fired_after_seconds": fired_after,
                "alert_notified_to_sink": notified,
                "sales_at_95_percent": _summary(manifest),
                "alert_resolved_after_seconds": resolved_after,
                "prometheus_disk_used_ratio": prom(f'max(disk_used_ratio{{store_id="{_DISK}"}})'),
                "filled_to_sales_done_seconds": round(time.monotonic() - filled_at),
            },
        )
        assert used / size >= 0.94
        assert fired_after is not None, "cảnh báo đĩa không phát ra"
        assert notified, "alert-sink không nhận được thông báo rs-store-disk"
        record = manifest.stores[_DISK]
        assert len(record.sales) == 30 and record.unknown == [] and record.rejected == []
    finally:
        EDGE.pop(_DISK, None)
        _compose_disk("rm", "-s", "-f", "-v", "edge-api-disk", "edge-db-disk")
        docker("volume", "rm", "-f", "retail-sync_edge_db_disk", check=False)


# ═══════════════════════════════ CH-5 ═══════════════════════════════


async def test_ch5_redis_down(stack: Stack) -> None:
    """Tắt Redis khi đang bán. Đạt: bán bình thường (mọi đơn `201`), chỉ chậm hơn (nếu có)."""
    store = "store-001"
    cache = container_of(f"edge-cache-{store}")
    before = await run_edge_plans(
        stack, "ch5-up", {store: sales_plan(duration=40, sales=40, seed=5)}
    )
    docker("stop", cache)
    try:
        health = (await asyncio.to_thread(_get, f"{EDGE[store][0]}/health")).json()
        down = await run_edge_plans(
            stack, "ch5-down", {store: sales_plan(duration=40, sales=40, seed=6)}
        )
    finally:
        docker("start", cache)
    settled = await audit_until_settled(
        down,
        edge_dsns=_dsns(stack, [store]),
        central_dsn=stack.central_dsn,
        wait_seconds=CONVERGE_SECONDS,
        poll_seconds=5,
    )
    report(
        "chaos",
        "CH-5",
        {
            "health_while_redis_down": health,
            "redis_up": _summary(before),
            "redis_down": _summary(down),
            "final_status": settled.status,
        },
    )
    record = down.stores[store]
    assert len(record.sales) == 40 and not record.unknown and not record.rejected
    assert settled.status == CONVERGED, settled.findings[:5]


# ═══════════════════════════════ CH-6 ═══════════════════════════════


async def test_ch6_ten_stores_reconnect_simultaneously(stack: Stack) -> None:
    """10 cửa hàng (ảo, docs/18 §3) mất mạng CÙNG LÚC rồi nối lại CÙNG LÚC với tồn đọng lớn.
    Đạt: trung tâm không sập (`/health` không lần nào hỏng), backpressure hoạt động (503/429 có
    `Retry-After`), mọi sự kiện tới đủ (`CONVERGED`, không dead-letter)."""
    if not VIRTUAL_KEYS.exists():
        pytest.fail(f"thiếu {VIRTUAL_KEYS} — make provision-virtual")
    targets = read_keys(VIRTUAL_KEYS.read_text(encoding="utf-8"))[:10]
    assert len(targets) == 10
    profile = load_profile("t2")
    days, rate = 2, 720.0  # một ngày giả lập 15 giờ ≈ 75 giây thật
    from datetime import UTC, datetime, timedelta

    from shared.config import SyncSettings
    from simulator.__main__ import _catalog
    from simulator.generator import plan_store
    from tests.scenarios.conftest import CRAWL_DATA

    catalog = _catalog(CRAWL_DATA)
    start = (datetime.now(UTC) + timedelta(hours=7)).date() - timedelta(days=days)
    plans = {
        t.store_id: plan_store(
            profile,
            store_id=t.store_id,
            catalog=list(catalog),
            prices=catalog,
            seed=66,
            nonce=uuid.uuid4().int % 10**8,
            days=days,
            rate=rate,
            start_date=start,
        )
        for t in targets
    }
    # Mọi cửa hàng offline từ 09:00 ngày đầu tới 20:00 ngày cuối rồi CÙNG nối lại.
    quirks = Quirks.parse(
        "offline",
        ["offline_share=1.0", "offline_from_day=0", "offline_from_hour=9",
         "offline_to_day=-1", "offline_to_hour=20"],
    )  # fmt: skip
    manifest = Manifest(
        run_id=f"ch6-{uuid.uuid4().hex[:6]}",
        mode="virtual",
        profile=profile.name,
        seed=66,
        nonce=0,
        days=days,
        rate=rate,
        started_at=datetime.now(UTC).isoformat(),
    )
    refused_before = prom("sum(ingest_batches_refused_total) or vector(0)") or 0.0
    probe = Probe(stack.central_url)
    probe.start()
    try:
        await run_virtual(
            targets,
            plans,
            central_url=stack.central_url,
            manifest=manifest,
            profile=profile,
            start_date=start,
            rate=rate,
            prices=catalog,
            quirks=quirks,
            offline_store_ids=[t.store_id for t in targets],
            settings=SyncSettings(_env_file=None),
            drain_timeout=900,
            seed=66,
        )
    finally:
        health = await probe.stop()
        manifest.write(ROOT / "runs" / manifest.run_id)
    await asyncio.sleep(40)  # hai chu kỳ đẩy metric (15 s) để counter tới Prometheus
    refused_after = prom("sum(ingest_batches_refused_total) or vector(0)") or 0.0
    by_reason = {
        reason: prom(f'sum(ingest_batches_refused_total{{reason="{reason}"}}) or vector(0)')
        for reason in ("overloaded", "rate_limited", "too_large")
    }
    settled = await audit_until_settled(
        manifest, edge_dsns={}, central_dsn=stack.central_dsn, wait_seconds=120, poll_seconds=5
    )
    sync = {
        k: sum(r.outbox.get(k, 0) for r in manifest.stores.values())
        for k in ("sent", "pending", "dead", "transport_errors")
    }
    report(
        "chaos",
        "CH-6",
        {
            "stores": len(targets),
            "sales": sum(len(r.sales) for r in manifest.stores.values()),
            "sync": sync,
            "central_health_probe": health,
            "refused_batches_during_run": refused_after - refused_before,
            "refused_total_by_reason": by_reason,
            "client_latency_ms": manifest.stats()["latency_ms"],
            "final_status": settled.status,
            "manifest": manifest.run_id,
        },
    )
    assert health["failed"] == 0, f"trung tâm không trả lời /health {health['failed']} lần"
    assert sync["pending"] == 0 and sync["dead"] == 0
    assert sync["transport_errors"] > 0 and refused_after > refused_before, (
        "không thấy backpressure — tải chưa đủ để thử nó"
    )
    assert settled.status == CONVERGED, settled.findings[:5]


# ═══════════════════════════════ CH-7 ═══════════════════════════════


async def test_ch7_resend_batch_ten_times(stack: Stack) -> None:
    """Gửi lặp CÙNG MỘT LÔ sự kiện thật (lấy từ outbox cửa hàng) 10 lần. Đạt: trung tâm nhận cả
    10 lần (`accepted` đủ), điểm cộng đúng một lần, không đơn nào nhân đôi."""
    store = "store-001"
    manifest = await run_edge_plans(
        stack, "ch7", {store: sales_plan(duration=30, sales=20, seed=7, new_share=0.5)}
    )
    settled = await audit_until_settled(
        manifest, edge_dsns=_dsns(stack, [store]), central_dsn=stack.central_dsn,
        wait_seconds=CONVERGE_SECONDS, poll_seconds=3,
    )  # fmt: skip
    assert settled.status == CONVERGED, settled.findings[:5]
    edge = await asyncpg.connect(edge_dsn(stack, store))
    try:
        rows = await edge.fetch(
            "SELECT payload FROM outbox WHERE created_at >= $1::timestamptz ORDER BY id LIMIT 200",
            __import__("datetime").datetime.fromisoformat(manifest.started_at),
        )
    finally:
        await edge.close()
    envelopes = [
        json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"] for r in rows
    ]
    ids = [uuid.UUID(e["event_id"]) for e in envelopes]
    customers = [uuid.UUID(c) for c in manifest.stores[store].customers]

    central = await asyncpg.connect(stack.central_dsn)
    client = HttpCentralClient(
        base_url=stack.central_url, api_key=stack.store_key, timeout_seconds=30
    )
    try:

        async def state() -> tuple[int, int, int]:
            ledger = await central.fetchval(
                "SELECT count(*) FROM point_ledger WHERE event_id = ANY($1::uuid[])", ids
            )
            balance = await central.fetchval(
                "SELECT COALESCE(sum(balance), 0) FROM point_balance"
                " WHERE customer_id = ANY($1::uuid[])",
                customers,
            )
            sales = await central.fetchval(
                "SELECT count(*) FROM sale_replica WHERE shift_id = ANY($1::uuid[])",
                _shift_ids(manifest, store),
            )
            return int(ledger), int(balance), int(sales)

        before = await state()
        accepted = []
        for _ in range(10):
            result = await client.push(envelopes)
            accepted.append(len(result.accepted))
            assert result.rejected == []
        after = await state()
    finally:
        await client.aclose()
        await central.close()
    final = await audit(manifest, edge_dsns=_dsns(stack, [store]), central_dsn=stack.central_dsn)
    report(
        "chaos",
        "CH-7",
        {
            "batch_size": len(envelopes),
            "accepted_each_time": accepted,
            "ledger_balance_sales_before": before,
            "ledger_balance_sales_after": after,
            "final_status": final.status,
            "manifest": manifest.run_id,
        },
    )
    assert accepted == [len(envelopes)] * 10
    assert before == after
    assert final.status == CONVERGED


async def test_ch7_virtual_stores_resend_every_batch_ten_times(stack: Stack) -> None:
    """Cùng CH-7 ở quy mô: 3 cửa hàng ảo, 30% lô bị gửi lại 10 lần (tật `resend`)."""
    keys = read_keys(VIRTUAL_KEYS.read_text(encoding="utf-8"))[10:13]
    from datetime import UTC, datetime, timedelta

    from simulator.__main__ import _catalog
    from simulator.generator import plan_store
    from tests.scenarios.conftest import CRAWL_DATA

    profile = load_profile("t1")
    catalog = _catalog(CRAWL_DATA)
    start = (datetime.now(UTC) + timedelta(hours=7)).date() - timedelta(days=1)
    plans = {
        t.store_id: plan_store(
            profile, store_id=t.store_id, catalog=list(catalog), prices=catalog, seed=77,
            nonce=uuid.uuid4().int % 10**8, days=1, rate=900.0, start_date=start,
        )
        for t in keys
    }  # fmt: skip
    manifest = Manifest(
        run_id=f"ch7v-{uuid.uuid4().hex[:6]}", mode="virtual", profile="t1", seed=77, nonce=0,
        days=1, rate=900.0, started_at=datetime.now(UTC).isoformat(),
    )  # fmt: skip
    await run_virtual(
        keys, plans, central_url=stack.central_url, manifest=manifest, profile=profile,
        start_date=start, rate=900.0, prices=catalog,
        quirks=Quirks.parse("resend", ["resend_share=0.3", "resend_times=10"]), seed=77,
    )  # fmt: skip
    manifest.write(ROOT / "runs" / manifest.run_id)
    settled = await audit_until_settled(
        manifest, edge_dsns={}, central_dsn=stack.central_dsn, wait_seconds=120, poll_seconds=5
    )
    stats = {
        k: sum(r.outbox.get(k, 0) for r in manifest.stores.values())
        for k in ("sent", "resent_batches", "resend_mismatch", "pending", "dead")
    }
    report("chaos", "CH-7-virtual", {"sync": stats, "final_status": settled.status})
    assert stats["resent_batches"] >= 10 and stats["resend_mismatch"] == 0
    assert settled.status == CONVERGED, settled.findings[:5]


def test_central_is_healthy_after_chaos() -> None:
    """Chạy CUỐI: sau cả loạt, mọi thành phần đã lên lại (không kịch bản nào để lại hậu quả)."""
    assert is_up("http://localhost:8000")
    for sid in ("store-001", "store-002", "store-003"):
        assert is_up(EDGE[sid][0]), sid
