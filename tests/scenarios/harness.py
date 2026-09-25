"""Công cụ chung cho test hỗn loạn (CH), tải (LD) và diễn tập cảnh báo trên compose THẬT.

Không có logic kiểm nào ở đây — chỉ những thao tác lặp lại: gây sự cố bằng `docker`, dựng lịch
ý định có chủ đích, chạy bộ giả lập ở nền, hỏi Prometheus/Grafana, ghi báo cáo JSON vào
`runs/chaos|load/<phiên>/` làm bằng chứng (docs/progress trích số từ đó).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from shared.seed_data import parse_products
from simulator.generator import CloseShift, CustomerRef, Intent, OpenShift, Sale
from simulator.manifest import Manifest
from simulator.sinks.edge_http import StoreTarget, run_edge
from tests.scenarios.conftest import CRAWL_DATA, PROJECT, ROOT, Stack, container

#: Cổng host của từng cửa hàng trong compose (edge + edge-multi).
EDGE = {
    "store-001": ("http://localhost:8001", 5433, "edge_store_001"),
    "store-002": ("http://localhost:8002", 5435, "edge_store_002"),
    "store-003": ("http://localhost:8003", 5436, "edge_store_003"),
}
NETWORK = f"{PROJECT}_default"
GRAFANA = "http://localhost:3001"
PROMETHEUS = f"{GRAFANA}/api/datasources/proxy/uid/prometheus/api/v1/query"
SESSION = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

PRODUCTS = {
    p.product_id: p.unit_price
    for p in parse_products(CRAWL_DATA / "products" / "product_details.csv")[:200]
    if 5_000 <= p.unit_price <= 300_000
}


def report(kind: str, name: str, data: dict[str, Any]) -> Path:
    """Ghi bằng chứng của một kịch bản: `runs/<kind>/<phiên>/<name>.json`."""
    out = ROOT / "runs" / kind / SESSION / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out


def docker(*args: str, check: bool = True, timeout: float = 180) -> str:
    r = subprocess.run(  # noqa: S603 — tham số dựng từ hằng của test
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)}: {r.stderr.strip()[:500]}")
    return r.stdout


def edge_dsn(stack: Stack, store_id: str) -> str:
    _, port, db = EDGE[store_id]
    e = stack.env
    return (
        f"postgresql://{e.get('EDGE_DB_USER', 'edge_app')}:{e['EDGE_DB_PASSWORD']}"
        f"@localhost:{port}/{db}"
    )


def is_up(url: str, *, timeout: float = 3) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


async def wait_up(url: str, *, within: float = 90) -> float:
    """Chờ `/health` trả 200; trả số giây đã chờ."""
    started = time.monotonic()
    while time.monotonic() - started < within:
        if await asyncio.to_thread(is_up, url, timeout=2):
            return time.monotonic() - started
        await asyncio.sleep(0.5)
    raise TimeoutError(f"{url} không lên sau {within}s")


def stores_up() -> list[str]:
    return [sid for sid, (url, _, _) in EDGE.items() if is_up(url)]


# ─────────────────────────────── Lịch ý định viết tay ───────────────────────────────


def sales_plan(
    *,
    duration: float,
    sales: int,
    seed: int,
    start: float = 2.0,
    tail: float = 5.0,
    new_share: float = 0.3,
    returning_share: float = 0.3,
) -> list[Intent]:
    """Mở ca lúc 0 → `sales` đơn rải đều ngẫu nhiên trong [start, duration - tail] → đóng ca.

    Khách: `new_share` đơn đăng ký khách mới (SĐT ngẫu nhiên, không trùng lần chạy trước),
    `returning_share` đơn là khách vừa đăng ký trong lần chạy, còn lại vãng lai."""
    rng = random.Random(seed)  # lịch test tái lập theo seed
    ids = list(PRODUCTS)
    known: list[CustomerRef] = []
    intents: list[Intent] = [OpenShift(at=0.0, day=0, shift_no=0)]
    times = sorted(rng.uniform(start, duration - tail) for _ in range(sales))
    for i, at in enumerate(times):
        roll = rng.random()
        customer: CustomerRef | None = None
        if roll < new_share:
            customer = CustomerRef(
                ref=f"c{uuid.uuid4().hex[:8]}", new=True, phone=f"09{rng.randrange(10**8):08d}"
            )
            known.append(CustomerRef(customer.ref, False, customer.phone))
        elif roll < new_share + returning_share and known:
            customer = rng.choice(known)
        lines = tuple((p, rng.randint(1, 3)) for p in rng.sample(ids, rng.randint(1, 4)))
        intents.append(
            Sale(
                at=at,
                intent_id=f"s{i:05d}",
                lines=lines,
                customer=customer,
                payment=rng.choice(["CASH", "CASH", "CARD", "SPLIT"]),
            )
        )
    intents.append(CloseShift(at=duration, day=0, shift_no=0))
    return intents


async def run_edge_plans(
    stack: Stack, run_id: str, plans: dict[str, list[Intent]], *, request_timeout: float = 15
) -> Manifest:
    manifest = Manifest(
        run_id=f"{run_id}-{uuid.uuid4().hex[:6]}",
        mode="edge",
        profile="chaos",
        seed=0,
        nonce=0,
        days=1,
        rate=1.0,
        started_at=datetime.now(UTC).isoformat(),
    )
    clients = {
        sid: httpx.AsyncClient(base_url=EDGE[sid][0], timeout=request_timeout) for sid in plans
    }
    try:
        await run_edge(
            [StoreTarget(sid, c) for sid, c in clients.items()],
            plans,
            password=stack.password,
            manifest=manifest,
            opening_cash=500_000,
        )
    finally:
        for c in clients.values():
            await c.aclose()
        manifest.write(ROOT / "runs" / manifest.run_id)
    return manifest


# ─────────────────────────────── Prometheus / Grafana ───────────────────────────────


def prom(query: str) -> float | None:
    """Giá trị vô hướng của một truy vấn PromQL (None = không có dữ liệu / không truy vấn được)."""
    try:
        r = httpx.get(PROMETHEUS, params={"query": query}, auth=("admin", "admin"), timeout=10)
        result = r.json()["data"]["result"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    if not result:
        return None
    return float(result[0]["value"][1])


ALERT_RULES = ROOT / "infra/observability/grafana/provisioning/alerting/retail-sync.yaml"


def alert_titles() -> dict[str, str]:
    """Tiêu đề rule → uid, từ file provisioning (JSON sau các dòng chú thích `#`)."""
    body = "\n".join(
        line
        for line in ALERT_RULES.read_text(encoding="utf-8").splitlines()
        if not line.startswith("#")
    )
    return {r["title"]: r["uid"] for r in json.loads(body)["groups"][0]["rules"]}


def alert_states() -> dict[str, str]:
    """uid của rule → trạng thái Grafana hiện tại (inactive | pending | firing)."""
    titles = alert_titles()
    r = httpx.get(
        f"{GRAFANA}/api/prometheus/grafana/api/v1/rules", auth=("admin", "admin"), timeout=10
    )
    out: dict[str, str] = {}
    for group in r.json()["data"]["groups"]:
        for rule in group["rules"]:
            out[titles.get(rule["name"], rule["name"])] = rule["state"]
    return out


async def wait_alert(uid: str, *, state: str = "firing", within: float = 900) -> float | None:
    """Chờ rule `uid` sang `state`. Trả số giây đã chờ, None nếu hết hạn."""
    started = time.monotonic()
    while time.monotonic() - started < within:
        try:
            if (await asyncio.to_thread(alert_states)).get(uid) == state:
                return time.monotonic() - started
        except (httpx.HTTPError, KeyError, ValueError):
            pass
        await asyncio.sleep(15)
    return None


def notified(uid: str, *, status: str = "firing") -> bool:
    """`alert-sink` đã NHẬN thông báo `status` của rule `uid` chưa (theo uid, hoặc theo tiêu đề
    với bản ghi cũ không có uid)."""
    title_of = {v: k for k, v in alert_titles().items()}
    text = httpx.get("http://localhost:8089/alerts", timeout=10).text
    for line in text.splitlines():
        row = json.loads(line)
        if row.get("status") != status:
            continue
        if row.get("rule_uid") == uid or row.get("alertname") == title_of.get(uid):
            return True
    return False


def observability_up() -> bool:
    try:
        return httpx.get(f"{GRAFANA}/api/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


@dataclass
class Probe:
    """Hỏi `/health` liên tục ở nền — "trung tâm không sập" đo được bằng số lần hỏng."""

    url: str
    ok: int = 0
    failed: int = 0
    worst_ms: float = 0.0
    _task: asyncio.Task[None] | None = None

    async def _loop(self) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            while True:
                t = time.perf_counter()
                try:
                    r = await client.get(f"{self.url}/health")
                    good = r.status_code == 200
                except httpx.HTTPError:
                    good = False
                ms = (time.perf_counter() - t) * 1000
                self.worst_ms = max(self.worst_ms, ms)
                if good:
                    self.ok += 1
                else:
                    self.failed += 1
                await asyncio.sleep(0.5)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> dict[str, Any]:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        return {"ok": self.ok, "failed": self.failed, "worst_ms": round(self.worst_ms, 1)}


def container_of(service: str) -> str:
    return container(service)


def env_minutes(name: str, default: float) -> float:
    return float(os.environ.get(name, default))
