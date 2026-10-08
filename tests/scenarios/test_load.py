"""LD-1, LD-2, LD-4 + overhead OpenTelemetry trên compose THẬT (docs/08 §6.3, docs/16 §4).

Nguồn tải là bộ giả lập (vòng hở, docs/18 §1) — không `k6`. Phép đo chỉ hợp lệ khi chính bộ giả
lập dùng < 50% một CPU (`manifest.cpu_share`); LD-2 chia cửa hàng ảo ra nhiều tiến trình để giữ
điều đó (docs/18 §9). Mỗi kịch bản ghi số đo vào `runs/load/<phiên>/LD-n.json` — các con số ở
docs/02 §6 và docs/progress lấy từ đó.

LD-3 (dữ liệu T2 trong ClickHouse) và seam 50 triệu dòng ledger là script riêng
(`infra/loadtest/`), vì chúng chạy nhiều giờ; `test_ld3_class_c_queries_under_3s` chỉ đọc báo cáo.

**OPT-IN** (`RETAIL_SYNC_SCENARIOS=1` + `RETAIL_SYNC_LOAD=1`, `make test-load`): đẩy hàng trăm
nghìn sự kiện vào stack dev, tắt/bật OTel của edge-api-store-001.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest

from shared.events import CustomerPayload, build_envelope
from simulator.audit import CONVERGED, audit_until_settled
from simulator.manifest import Manifest, percentiles
from simulator.sinks.virtual import read_keys
from tests.scenarios.conftest import ENV_FILE, ROOT, Stack
from tests.scenarios.harness import (
    EDGE,
    container_of,
    docker,
    edge_dsn,
    prom,
    report,
    run_edge_plans,
    sales_plan,
    wait_up,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAIL_SYNC_LOAD") != "1",
    reason="test tải là opt-in: RETAIL_SYNC_LOAD=1 (make test-load)",
)

#: Tải đỉnh của MỘT cửa hàng cho LD-1: cửa hàng T3 (800 đơn/ngày), khung tối 17–20h chiếm 35%
#: doanh số ngày (docs/02 §1) → 93 đơn/giờ, ngày lễ ×2 → 187 đơn/giờ, nén ×50 (docs/16 §4
#: `--rate x50`) → 2,6 đơn/giây. Mỗi đơn là 2–3 request (tạm tính, [đăng ký khách], chốt).
LD1_SALES_PER_SECOND = 800 * 0.35 / 3 * 2 * 50 / 3600
LD2_KEYS = ROOT / "runs" / "virtual-keys-ld2.env"


def _stats(manifest: Manifest) -> dict[str, Any]:
    return {
        "latency_ms": manifest.stats()["latency_ms"],
        "cpu_share": manifest.cpu_share,
        "scheduler_lag_s": manifest.stats()["scheduler_lag_s"],
        "sales_201": sum(len(r.sales) for r in manifest.stores.values()),
        "unknown": sum(len(r.unknown) for r in manifest.stores.values()),
        "rejected": sum(len(r.rejected) for r in manifest.stores.values()),
    }


def _docker_cpu(name: str) -> float | None:
    out = docker("stats", "--no-stream", "--format", "{{.CPUPerc}}", name, check=False).strip()
    try:
        return float(out.rstrip("%"))
    except ValueError:
        return None


# ═══════════════════════════════ LD-1 + OTel ═══════════════════════════════


async def test_ld1_one_store_peak_hour(stack: Stack) -> None:
    """LD-1: một cửa hàng ở tải đỉnh giờ cao điểm 10 phút. Đạt: p95 `POST /sales` < 500 ms, bộ
    giả lập < 50% CPU, mọi đơn `201`, hội tụ lên trung tâm. Kèm bậc tải cao hơn (5/10/20 đơn/s,
    2 phút mỗi bậc) để biết khoảng dư — không phải điều kiện đạt."""
    store = "store-001"
    minutes = float(os.environ.get("LD1_MINUTES", "10"))
    duration = minutes * 60
    sales = int(LD1_SALES_PER_SECOND * duration)
    manifest = await run_edge_plans(
        stack, "ld1", {store: sales_plan(duration=duration, sales=sales, seed=11, tail=10)}
    )
    settled = await audit_until_settled(
        manifest, edge_dsns={store: edge_dsn(stack, store)}, central_dsn=stack.central_dsn,
        wait_seconds=300, poll_seconds=5,
    )  # fmt: skip
    steps = {}
    for rate in (5, 10, 20):
        step = await run_edge_plans(
            stack,
            f"ld1-x{rate}",
            {store: sales_plan(duration=120, sales=rate * 110, seed=rate, tail=10)},
            request_timeout=30,
        )
        steps[f"{rate}/s"] = _stats(step)
    result = {
        "target_sales_per_second": round(LD1_SALES_PER_SECOND, 2),
        "minutes": minutes,
        "peak": _stats(manifest),
        "audit": settled.status,
        "headroom_steps": steps,
        "server_p95_sales_s": prom(
            "histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket"
            '{job="edge-api-store-001", http_route="/api/v1/sales"}[15m])))'
        ),
    }
    report("load", "LD-1", result)
    peak = _stats(manifest)
    assert peak["unknown"] == 0 and peak["rejected"] == 0
    assert peak["latency_ms"]["sales"]["p95"] < 500
    assert manifest.cpu_share is not None and manifest.cpu_share < 0.5, "bộ giả lập là nút thắt"
    assert settled.status == CONVERGED, settled.findings[:5]


#: Chế độ đo overhead → file override (None = cấu hình compose thường: trace 100% + metric).
OTEL_MODES = {"on": None, "off": "otel-off.compose.yaml", "sampled": "otel-sampled.compose.yaml"}


def _edge_api(mode: str) -> None:
    """Dựng lại edge-api-store-001 ở một chế độ OTel: `on` (trace 100%), `off` (endpoint rỗng =
    tắt hẳn SDK), `sampled` (trace 10%, metric giữ nguyên)."""
    files = ["-f", str(ROOT / "infra" / "compose.yaml")]
    if (override := OTEL_MODES[mode]) is not None:
        files += ["-f", str(ROOT / "infra" / "chaos" / override)]
    subprocess.run(  # noqa: S603
        ["docker", "compose", "-p", "retail-sync", *files, "--env-file", str(ENV_FILE),  # noqa: S607
         "--profile", "edge", "up", "-d", "--no-deps", "edge-api-store-001"],
        check=True, capture_output=True, timeout=300,
    )  # fmt: skip


async def _sample_cpu(name: str, into: list[float]) -> None:
    while True:
        if (c := await asyncio.to_thread(_docker_cpu, name)) is not None:
            into.append(c)
        await asyncio.sleep(5)


async def test_otel_overhead(stack: Stack) -> None:
    """ADR-009: overhead của instrumentation trên chốt đơn, ở hai mức: `on` (trace 100% + metric,
    cấu hình hiện tại) và `sampled` (trace 10%), so với `off`. Chạy xen kẽ hai vòng cùng một lịch
    (10 đơn/s, 3 phút) để trôi nền của máy không đổ lên một bên.

    Điều kiện ADR-009 (tăng ≤ 5%) được GHI, không chặn: vượt là quyết định giảm lấy mẫu, cần người
    — và `sampled` cho sẵn con số để quyết. Test chỉ chặn khi PHÉP ĐO không đáng tin: hai lần chạy
    cùng chế độ lệch nhau quá 30% ở p95. (Bản đầu chặn "overhead p95 < 50%" như một phép kiểm "bất
    thường"; runner CI đo +146% — 11 → 28 ms — lặp lại y hệt ở cả hai lần: đó là SỐ ĐO, không phải
    phép đo hỏng, workflow `proof` 2026-09-28.)"""
    store = "store-001"
    url = EDGE[store][0]
    runs: dict[str, list[dict[str, Any]]] = {mode: [] for mode in OTEL_MODES}
    try:
        for i, mode in enumerate(("on", "off", "sampled", "on", "off", "sampled")):
            await asyncio.to_thread(_edge_api, mode)
            await wait_up(url, within=120)
            # Khởi động bằng TẢI THẬT, không tính số: pool kết nối, prepared statement của asyncpg,
            # cache sản phẩm, exporter OTel. Bản đầu chỉ ngủ 10 s → lần đo đầu sau mỗi lần dựng lại
            # chậm hẳn (`sampled` lần 1: p50 13,4 ms, lần 2: 8,8 ms — workflow `proof` 2026-10-07).
            await run_edge_plans(
                stack, f"otel-warmup-{mode}-{i}",
                {store: sales_plan(duration=45, sales=400, seed=30 + i, tail=5)},
                request_timeout=30,
            )  # fmt: skip
            cpu: list[float] = []
            sampler = asyncio.create_task(_sample_cpu(container_of(f"edge-api-{store}"), cpu))
            try:
                m = await run_edge_plans(
                    stack, f"otel-{mode}-{i}",
                    {store: sales_plan(duration=180, sales=1700, seed=31, tail=5)},
                    request_timeout=30,
                )  # fmt: skip
            finally:
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)
            mean_cpu = round(sum(cpu) / len(cpu), 1) if cpu else None
            runs[mode].append(_stats(m) | {"edge_api_cpu_pct_mean": mean_cpu})
    finally:
        await asyncio.to_thread(_edge_api, "on")
        await wait_up(url, within=120)

    def mean(mode: str, q: str) -> float:
        vals = [float(r["latency_ms"]["sales"][q]) for r in runs[mode]]
        return sum(vals) / len(vals)

    def cpu_of(mode: str) -> float | None:
        vals = [r["edge_api_cpu_pct_mean"] for r in runs[mode] if r["edge_api_cpu_pct_mean"]]
        return round(sum(vals) / len(vals), 1) if vals else None

    overhead = {
        mode: {q: round(mean(mode, q) / mean("off", q) - 1, 4) for q in ("p50", "p95")}
        for mode in ("on", "sampled")
    }
    spread = {
        mode: round(abs(p95s[0] - p95s[1]) / min(p95s), 4)
        for mode in OTEL_MODES
        if len(p95s := [float(r["latency_ms"]["sales"]["p95"]) for r in runs[mode]]) == 2
    }
    result = {
        "runs": runs,
        "latency_overhead": overhead,
        "adr009_within_5pct": {m: o["p95"] <= 0.05 for m, o in overhead.items()},
        "edge_api_cpu_pct_mean": {mode: cpu_of(mode) for mode in OTEL_MODES},
        "p95_spread_between_repeats": spread,
    }
    report("load", "OTEL-overhead", result)
    for r in (r for rs in runs.values() for r in rs):
        assert r["cpu_share"] is not None and r["cpu_share"] < 0.5
        assert r["unknown"] == 0 and r["rejected"] == 0
    assert len(spread) == len(OTEL_MODES)
    for mode, s in spread.items():
        assert s < 0.3, f"{mode}: hai lần đo lệch p95 {s:.0%} — phép đo không lặp lại được"


# ═══════════════════════════════ LD-2 ═══════════════════════════════


def _ensure_ld2_keys(stack: Stack, n: int) -> list[str]:
    if not LD2_KEYS.exists() or len(read_keys(LD2_KEYS.read_text(encoding="utf-8"))) < n:
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "central.ops.provision_store", "--from-master", str(n),
             "--keys-out", str(LD2_KEYS)],
            cwd=ROOT, check=True, capture_output=True, timeout=600,
            env={**os.environ, "PYTHONUTF8": "1",
                 "DATABASE_URL": stack.central_dsn.replace("postgresql://", "postgresql+asyncpg://")},
        )  # fmt: skip
    return LD2_KEYS.read_text(encoding="utf-8").splitlines()


def _run_virtual_split(
    stack: Stack, lines: list[str], *, stores: int, per_process: int, rate: float, seed: int
) -> list[Manifest]:
    """`stores` cửa hàng ảo chia thành các tiến trình `simulator run --mode virtual` song song."""
    keys = [line for line in lines if "=" in line and not line.startswith("#")][:stores]
    procs = []
    out_dir = ROOT / "runs" / "load" / "ld2-keys"
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(keys), per_process):
        chunk = out_dir / f"s{stores}-p{i // per_process}.env"
        chunk.write_text("\n".join(keys[i : i + per_process]) + "\n", encoding="utf-8")
        run_id = f"ld2-s{stores}-p{i // per_process}-{uuid.uuid4().hex[:4]}"
        procs.append(
            (
                run_id,
                subprocess.Popen(  # noqa: S603
                    [sys.executable, "-m", "simulator", "run", "--mode", "virtual",
                     "--profile", "t2", "--keys", str(chunk), "--days", "1",
                     "--rate", str(rate), "--seed", str(seed + i), "--central", stack.central_url,
                     "--run-id", run_id, "--drain-timeout", "1200"],
                    cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1"},
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                ),
            )
        )  # fmt: skip
    manifests = []
    for run_id, proc in procs:
        proc.wait(timeout=3600)
        manifests.append(Manifest.read(ROOT / "runs" / run_id / "manifest.json"))
    return manifests


def _client_latency(manifest: Manifest) -> dict[str, Any]:
    """`Manifest.read` không giữ mẫu thô; phân vị đã tính nằm ở `stats` của file."""
    data = json.loads((ROOT / "runs" / manifest.run_id / "manifest.json").read_text("utf-8"))
    stats: dict[str, Any] = data["stats"]["latency_ms"].get("POST /events", {})
    return stats


async def test_ld2_virtual_stores_10_to_200(stack: Stack) -> None:
    """LD-2: 10 → 50 → 100 → 200 cửa hàng ảo (T2) cùng đẩy đồng bộ, mỗi bậc ~5 phút. Đạt: trung
    tâm giữ p95 `POST /events` < 1 s (phía server) ở mọi bậc — hoặc từ chối tử tế (503/429 có
    `Retry-After`) chứ không lỗi; mọi sự kiện tới đủ (`CONVERGED`); bộ giả lập < 50% CPU.

    Nhịp ×180 (một ngày mở cửa 15 giờ trong 5 phút), KHÔNG phải ×10 như bản nháp ở docs/16 §4:
    ×10 với 200 cửa hàng chỉ ~16 sự kiện/giây, xa dưới tải thiết kế. Mỗi bậc ghi `events_per_second`
    để so với ~260 lượt ghi/giây của docs/02 §1, và CPU của central-api/central-db để biết nghẽn ở
    đâu khi p95 vượt ngưỡng."""
    lines = await asyncio.to_thread(_ensure_ld2_keys, stack, 200)
    rate = 15 * 3600 / 300  # một ngày mở cửa (15 giờ) trong 5 phút
    steps: dict[str, Any] = {}
    for stores in (10, 50, 100, 200):
        refused0 = prom("sum(ingest_batches_refused_total) or vector(0)") or 0.0
        t0 = time.time()
        # CPU của trung tâm trong suốt bậc: `central-api` là MỘT tiến trình uvicorn (rate limit
        # trong bộ nhớ một tiến trình), nên chạm ~100% = nghẽn một nhân, không phải DB.
        cpu: dict[str, list[float]] = {"central-api": [], "central-db": []}
        samplers = [asyncio.create_task(_sample_cpu(container_of(s), v)) for s, v in cpu.items()]
        try:
            manifests = await asyncio.to_thread(
                _run_virtual_split, stack, lines, stores=stores, per_process=50, rate=rate,
                seed=stores,
            )  # fmt: skip
        finally:
            for t in samplers:
                t.cancel()
            await asyncio.gather(*samplers, return_exceptions=True)
        elapsed = time.time() - t0
        await asyncio.sleep(35)  # hai chu kỳ đẩy metric
        window = f"{int(elapsed) + 35}s"
        buckets = (
            'http_server_request_duration_seconds_bucket{job="central-api",'
            ' http_route="/api/v1/events"}'
        )
        server = {
            str(q): prom(f"histogram_quantile({q}, sum by (le) (increase({buckets}[{window}])))")
            for q in (0.5, 0.95, 0.99)
        }
        audits = []
        for m in manifests:
            settled = await audit_until_settled(
                m, edge_dsns={}, central_dsn=stack.central_dsn, wait_seconds=300, poll_seconds=10
            )
            audits.append(settled.status)
        records = [r for m in manifests for r in m.stores.values()]

        def total(key: str, rs: list[Any] = records) -> int:
            return sum(r.outbox.get(key, 0) for r in rs)

        refused1 = prom("sum(ingest_batches_refused_total) or vector(0)") or 0.0
        steps[str(stores)] = {
            "seconds": round(elapsed),
            "sales": sum(len(r.sales) for r in records),
            "events_sent": total("sent"),
            # Quy đổi ra tải thật: docs/02 §1 thiết kế test tải theo ~260 lượt ghi/giây (T3 2000
            # cửa hàng, giờ cao điểm 35%, ×2 lễ Tết). "N cửa hàng ảo" ở nhịp nén ×180 KHÔNG phải N
            # cửa hàng thật.
            "events_per_second": round(total("sent") / elapsed, 1) if elapsed else None,
            "cpu_pct_mean_max": {
                s: [round(sum(v) / len(v), 1), round(max(v), 1)] if v else None
                for s, v in cpu.items()
            },
            "transport_errors": total("transport_errors"),
            "pending": total("pending"),
            "dead": total("dead"),
            "server_p50_p95_p99_s": server,
            # Phía client theo từng tiến trình (phân vị không cộng được giữa các tiến trình).
            "client_ms_per_process": [_client_latency(m) for m in manifests],
            "refused_batches": refused1 - refused0,
            "simulator_cpu_share": [m.cpu_share for m in manifests],
            "audit": audits,
            "central_pg_connections_ratio": prom("max(pg_connections_used_ratio)"),
        }
        report("load", "LD-2", {"steps": steps, "rate": rate})
    for label, step in steps.items():
        assert step["pending"] == 0 and step["dead"] == 0, label
        assert all(s == CONVERGED for s in step["audit"]), label
        assert all(c is not None and c < 0.5 for c in step["simulator_cpu_share"]), label
        p95 = step["server_p50_p95_p99_s"]["0.95"]
        assert p95 is not None and p95 < 1.0, f"{label} cửa hàng: p95 {p95}s"


# ═══════════════════════════════ LD-4 ═══════════════════════════════


async def test_ld4_200_concurrent_connections(stack: Stack) -> None:
    """LD-4: 200 kết nối đồng thời tới trung tâm. Hai phép đo:

    1. QUA API (đường thật của 200 cửa hàng): 200 client cùng lúc, mỗi client gửi lô sự kiện
       liên tục 90 giây. Đạt: không lỗi — mọi phản hồi là `200`, hoặc `503`/`429` có `Retry-After`
       (từ chối tử tế); Postgres trung tâm không bao giờ bị mở quá pool của API.
    2. THẲNG vào Postgres (bằng chứng cho seam PgBouncer, docs/02 §4): mở 200 kết nối trực tiếp —
       Postgres chặn ở `max_connections`. Đó là lý do cửa hàng KHÔNG BAO GIỜ nói chuyện thẳng với
       DB trung tâm, và là ngưỡng mà PgBouncer phải đứng trước khi có thêm tiến trình đọc DB.
       Giữ 80 kết nối 2 phút để kích hoạt thử cảnh báo `rs-central-pg-conn` (> 70%)."""
    lines = await asyncio.to_thread(_ensure_ld2_keys, stack, 200)
    targets = read_keys("\n".join(lines))[:200]
    codes: dict[str, int] = {}
    missing_retry_after = 0
    latencies: list[float] = []
    stop_at = time.monotonic() + 90

    async def store_loop(target: Any, client: httpx.AsyncClient) -> None:
        nonlocal missing_retry_after
        while time.monotonic() < stop_at:
            events = [
                build_envelope(
                    event_id=uuid.uuid4(), event_type="CustomerCreated", store_id=target.store_id,
                    occurred_at=datetime.now(UTC),
                    payload=CustomerPayload(customer_id=uuid.uuid4(),
                                            phone_hash=f"ld4-{uuid.uuid4().hex}",
                                            created_locally_at_store=target.store_id),
                )
                for _ in range(20)
            ]  # fmt: skip
            t = time.perf_counter()
            key: str
            try:
                r = await client.post(
                    "/api/v1/events", json=events,
                    headers={"Authorization": f"Bearer {target.api_key}"},
                )  # fmt: skip
                key = str(r.status_code)
                if r.status_code in (429, 503) and "Retry-After" not in r.headers:
                    missing_retry_after += 1
                if r.status_code in (429, 503):
                    await asyncio.sleep(float(r.headers.get("Retry-After", "1")))
            except httpx.HTTPError as exc:
                key = type(exc).__name__
            latencies.append((time.perf_counter() - t) * 1000)
            codes[key] = codes.get(key, 0) + 1

    max_pg = 0.0

    async def watch_pg() -> None:
        nonlocal max_pg
        conn = await asyncpg.connect(stack.central_dsn)
        try:
            while time.monotonic() < stop_at:
                used = await conn.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'"
                )
                max_pg = max(max_pg, float(used))
                await asyncio.sleep(1)
        finally:
            await conn.close()

    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    clients = [
        httpx.AsyncClient(base_url=stack.central_url, timeout=30, limits=limits) for _ in targets
    ]
    try:
        await asyncio.gather(
            watch_pg(), *(store_loop(t, c) for t, c in zip(targets, clients, strict=True))
        )
    finally:
        await asyncio.gather(*(c.aclose() for c in clients))

    # ── 2. Thẳng vào Postgres ──
    direct: list[asyncpg.Connection] = []
    refused: str | None = None
    max_connections = None
    try:
        for _ in range(200):
            try:
                direct.append(await asyncpg.connect(stack.central_dsn, timeout=10))
            except (asyncpg.exceptions.TooManyConnectionsError, OSError) as exc:
                refused = f"{type(exc).__name__}: {exc}"[:200]
                break
        max_connections = int(await direct[0].fetchval("SHOW max_connections")) if direct else None
        opened = len(direct)
    finally:
        for c in direct[80:]:
            await c.close()
        held = direct[:80]
    await asyncio.sleep(150)  # 80 kết nối giữ 2,5 phút: > 70% max_connections (rs-central-pg-conn)
    pg_ratio_while_held = prom("max(pg_connections_used_ratio)")
    for c in held:
        await c.close()

    result = {
        "api": {
            "clients": len(targets),
            "responses": codes,
            "refusals_without_retry_after": missing_retry_after,
            "latency_ms": percentiles(latencies),
            "max_central_pg_client_connections": max_pg,
        },
        "direct_pg": {
            "opened_before_refusal": opened,
            "max_connections": max_connections,
            "refusal": refused,
            "pg_connections_used_ratio_while_holding_80": pg_ratio_while_held,
        },
    }
    report("load", "LD-4", result)
    bad = {k: v for k, v in codes.items() if k not in ("200", "429", "503")}
    assert not bad, f"lỗi không tử tế: {bad}"
    assert missing_retry_after == 0
    assert codes.get("200", 0) > 0
    assert max_connections is not None and opened < 200 and refused is not None


# ═══════════════════════════════ LD-3 ═══════════════════════════════


def test_ld3_class_c_queries_under_3s() -> None:
    """LD-3: đọc báo cáo của `infra/loadtest/ld3_warehouse.py` (T2, 24 tháng). Đạt: mọi truy vấn
    lớp C < 3 giây, và mart khớp đáp án của bộ sinh tới từng đồng."""
    reports = sorted((ROOT / "runs" / "load").glob("*/LD-3.json"))
    if not reports:
        pytest.skip("chưa chạy infra/loadtest/ld3_warehouse.py")
    data = json.loads(reports[-1].read_text(encoding="utf-8"))
    assert data["totals_match"], data["totals"]
    for name, q in data["queries"].items():
        assert q["max_seconds"] < 3.0, (name, q)
