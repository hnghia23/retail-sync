"""Khôi phục THẬT database cửa hàng từ backup (`infra/store_backup.py restore`, B02 I06, RPO ≤ 24h).

`verify` (khôi phục thử vào database tạm) đã chạy từ giai đoạn B việc 3; test này làm đúng việc mà
người vận hành làm khi máy cửa hàng hỏng — thay database thật — rồi kiểm hai hệ quả mà docstring
của script hứa trước:

1. Đơn SAU thời điểm backup là mất ở cửa hàng, nhưng đơn đã đồng bộ vẫn còn ở trung tâm (RPO).
2. Sự kiện chưa gửi lúc backup nằm lại trong outbox và được gửi LẠI sau khi khôi phục — vô hại,
   trung tâm trả `accepted` nhờ idempotency (AT-03), đối soát vẫn `CONVERGED`.

Và điều không ai hứa nhưng phải đúng: cửa hàng BÁN TIẾP được ngay sau khi khôi phục, và đơn mới lên
trung tâm bình thường (không va khóa/số thứ tự với đơn đã mất ở cửa hàng mà trung tâm vẫn giữ).

OPT-IN như test hỗn loạn (`RETAIL_SYNC_CHAOS=1`): thay database thật của store-001.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid

import asyncpg
import pytest

from simulator.audit import CONVERGED, audit_until_settled
from simulator.generator import Intent
from simulator.manifest import Manifest
from tests.scenarios.conftest import PROJECT, ROOT, Stack
from tests.scenarios.harness import (
    EDGE,
    container_of,
    docker,
    edge_dsn,
    report,
    run_edge_plans,
    sales_plan,
    wait_up,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAIL_SYNC_CHAOS") != "1",
    reason="khôi phục thật thay database cửa hàng — opt-in: RETAIL_SYNC_CHAOS=1",
)

STORE = "store-001"
SALES = 8


def _backup(*args: str) -> str:
    r = subprocess.run(  # noqa: S603
        [sys.executable, "infra/store_backup.py", *args, "--store", STORE, "--project", PROJECT],
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        check=True,
    )
    return r.stdout


def _dumps() -> list[str]:
    return sorted(line for line in _backup("list").split() if line.endswith(".dump"))


def _fresh_backup() -> str:
    """Sidecar dump ngay khi khởi động → khởi động lại nó là "backup ngay bây giờ"."""
    before = set(_dumps())
    docker("restart", container_of(f"edge-backup-{STORE}"))
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if new := sorted(set(_dumps()) - before):
            return new[-1]
        time.sleep(3)
    raise TimeoutError("sidecar backup không tạo bản mới trong 3 phút")


async def _count(dsn: str, sql: str, ids: list[str]) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        return int(await conn.fetchval(sql, [uuid.UUID(i) for i in ids]))
    finally:
        await conn.close()


def _plan(seed: int) -> list[Intent]:
    return sales_plan(duration=40, sales=SALES, seed=seed, tail=5)


async def _settle(stack: Stack, manifest: Manifest) -> str:
    settled = await audit_until_settled(
        manifest, edge_dsns={STORE: edge_dsn(stack, STORE)}, central_dsn=stack.central_dsn,
        wait_seconds=300, poll_seconds=3,
    )  # fmt: skip
    return settled.status


async def test_restore_store_database_from_backup(stack: Stack) -> None:
    worker = container_of(f"edge-sync-worker-{STORE}")

    # B: bán khi sync worker đang tắt → lúc backup, sự kiện của B còn CHƯA GỬI trong outbox.
    docker("stop", worker)
    try:
        b = await run_edge_plans(stack, "restore-b", {STORE: _plan(71)})
        dump = await asyncio.to_thread(_fresh_backup)
    finally:
        docker("start", worker)
    assert await _settle(stack, b) == CONVERGED
    verify = await asyncio.to_thread(_backup, "verify", "--file", dump)

    # C: bán SAU backup, đồng bộ xong — sẽ mất ở cửa hàng, còn ở trung tâm.
    c = await run_edge_plans(stack, "restore-c", {STORE: _plan(72)})
    assert await _settle(stack, c) == CONVERGED
    c_ids = list(c.stores[STORE].sales)

    t0 = time.monotonic()
    restored = await asyncio.to_thread(_backup, "restore", "--file", dump, "--yes")
    await wait_up(EDGE[STORE][0], within=180)
    restore_seconds = time.monotonic() - t0

    edge = edge_dsn(stack, STORE)
    c_at_store = await _count(edge, "SELECT count(*) FROM sale WHERE sale_id = ANY($1)", c_ids)
    c_at_central = await _count(
        stack.central_dsn, "SELECT count(*) FROM sale_replica WHERE sale_id = ANY($1)", c_ids
    )
    # Sự kiện của B được gửi lại từ outbox đã khôi phục: trung tâm nhận trùng, đối soát vẫn khớp.
    b_after = await _settle(stack, b)

    # E: bán tiếp ngay sau khi khôi phục.
    e = await run_edge_plans(stack, "restore-e", {STORE: _plan(73)})
    e_status = await _settle(stack, e)

    report("chaos", "RESTORE", {
        "dump": dump, "verify": verify[-600:], "restore": restored[-600:],
        "restore_seconds": round(restore_seconds, 1),
        "sales_after_backup": len(c_ids), "lost_at_store": len(c_ids) - c_at_store,
        "still_at_central": c_at_central, "resent_batch_audit": b_after,
        "sales_after_restore": len(e.stores[STORE].sales), "after_restore_audit": e_status,
        "manifests": [b.run_id, c.run_id, e.run_id],
    })  # fmt: skip
    assert c_at_store == 0, "bản backup chụp TRƯỚC các đơn C — chúng không thể còn ở cửa hàng"
    assert c_at_central == len(c_ids), "đơn đã đồng bộ phải còn nguyên ở trung tâm (RPO)"
    assert b_after == CONVERGED
    assert len(e.stores[STORE].sales) == SALES and e_status == CONVERGED
