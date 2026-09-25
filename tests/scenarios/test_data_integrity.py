"""AT-07, AT-10, DI-1…3 trên compose thật (docs/16 §2, §5).

DI-* là các ô của bảng đối soát xuyên tầng (docs/17 §5): một lần `simulator audit` kiểm hết.
Ở đây gọi tên từng ô bằng SQL trực tiếp, để khi đỏ biết ngay ô nào.
"""
# SQL chỉ ghép hằng của test.

from __future__ import annotations

import json
import os
import subprocess
import time

import asyncpg
import httpx
import pytest

from tests.scenarios.conftest import ROOT, Stack, container

SCHEDULER = container("airflow-scheduler")


def _airflow(*args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["docker", "exec", SCHEDULER, "airflow", *args],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
        env=os.environ | {"MSYS_NO_PATHCONV": "1"},
    )
    return result.stdout


def _run_dag(timeout: float = 900) -> str:
    """Kích một lượt `retail_pipeline` và chờ nó xong. Trả trạng thái cuối."""
    before = {
        r["run_id"]
        for r in json.loads(_airflow("dags", "list-runs", "retail_pipeline", "-o", "json"))
    }
    _airflow("dags", "trigger", "retail_pipeline")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = json.loads(_airflow("dags", "list-runs", "retail_pipeline", "-o", "json"))
        mine = [r for r in runs if r["run_id"] not in before and r["run_id"].startswith("manual__")]
        if mine and mine[0]["state"] in ("success", "failed"):
            return str(mine[0]["state"])
        time.sleep(10)
    raise TimeoutError("DAG retail_pipeline không xong trong thời hạn")


def _ch(stack: Stack, sql: str) -> list[list[str]]:
    e = stack.env
    r = httpx.post(
        "http://localhost:8123/",
        params={
            "user": e.get("CLICKHOUSE_USER", "dw"),
            "password": e["CLICKHOUSE_PASSWORD"],
            "database": "dw",
        },
        content=f"{sql} FORMAT TSV".encode(),
        timeout=60,
    )
    r.raise_for_status()
    return [line.split("\t") for line in r.text.splitlines() if line]


def _warehouse(stack: Stack) -> tuple[list[list[str]], list[list[str]]]:
    """Mọi số ở docs/17 §5 phía warehouse + TÊN PART của fact (part bị thay = tên đổi)."""
    numbers = _ch(
        stack,
        "SELECT (SELECT count() FROM bronze_sale), (SELECT count() FROM bronze_point_ledger),"
        " (SELECT count() FROM fact_sale_line), (SELECT sum(net_amount) FROM fact_sale_line),"
        " (SELECT count() FROM fact_payment), (SELECT sum(amount) FROM fact_payment),"
        " (SELECT count() FROM fact_point_event), (SELECT sum(delta) FROM fact_point_event)",
    )
    parts = _ch(
        stack,
        "SELECT table, name FROM system.parts WHERE database = 'dw' AND active"
        " AND table IN ('fact_sale_line', 'fact_payment', 'fact_point_event') ORDER BY table, name",
    )
    return numbers, parts


def _loaded_until(stack: Stack) -> str:
    """Mép "đã nạp tới" (cùng định nghĩa với macro dbt `loaded_until()`)."""
    [[until]] = _ch(
        stack,
        "SELECT toString(min(m)) FROM (SELECT table_name, max(window_end) AS m FROM bronze_load_log"
        " WHERE table_name IN ('sale', 'sale_line', 'sale_payment', 'point_ledger', 'shift',"
        " 'customer', 'point_balance') GROUP BY table_name)",
    )
    return until


def test_pipeline_idempotent_on_rerun(stack: Stack) -> None:
    """AT-07 — chạy DAG hai lần liên tiếp: mọi số không đổi, và không phân vùng fact nào bị thay
    (không có dòng mới thì không có tháng nào "bị ảnh hưởng", bẫy 5).

    "Không có dòng mới" là TIỀN ĐỀ của phép thử, không phải điều nó chứng minh. Cửa sổ trích xuất
    đóng theo giờ, nên nếu mốc giờ rơi vào giữa hai lượt thì lượt sau có dữ liệu mới HỢP LỆ (đơn
    của các test trước, vừa tới cửa sổ đã đóng). Bản đầu so thẳng hai lượt và đỏ đúng lúc CI dựng
    stack mới qua mốc 04:00 (2026-09-25). Giờ: một lượt bắt kịp trước, rồi chỉ so chặt khi mép
    "đã nạp tới" KHÔNG đổi giữa hai lượt; mép đổi thì tiền đề hỏng → thử cặp khác.
    """
    assert _run_dag() == "success"  # bắt kịp mọi cửa sổ đã đóng
    for _ in range(3):
        until, first = _loaded_until(stack), _warehouse(stack)
        assert _run_dag() == "success"
        if _loaded_until(stack) == until:
            assert _warehouse(stack) == first
            return
    pytest.fail("3 lần liền có cửa sổ mới đóng giữa hai lượt DAG — không thử được AT-07")


def test_ledger_matches_balance(stack: Stack) -> None:
    """AT-10 — job đối soát INV-4 (quét TOÀN BỘ, `--full`) khớp tuyệt đối: drift = 0."""
    e = stack.env
    url = (
        f"postgresql+asyncpg://{e.get('CENTRAL_DB_USER', 'central_app')}"
        f":{e['CENTRAL_DB_PASSWORD']}@localhost:5434/central"
    )
    result = subprocess.run(
        ["uv", "run", "python", "-m", "central.ops.reconcile", "--full"],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        cwd=ROOT,
        env=os.environ | {"DATABASE_URL": url, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert " drift=0" in result.stdout


async def test_central_replicas_hold_the_money_invariants(stack: Stack) -> None:
    """DI-1, DI-2, DI-3 ở trung tâm, trên TOÀN BỘ dữ liệu (mọi lần chạy, mọi cửa hàng)."""
    central = await asyncpg.connect(stack.central_dsn)
    try:
        di1 = await central.fetchval(
            "SELECT count(*) FROM point_balance b WHERE b.balance <>"
            " (SELECT COALESCE(sum(delta), 0) FROM point_ledger l"
            " WHERE l.customer_id = b.customer_id)"
        )
        di2 = await central.fetchval(
            "SELECT count(*) FROM sale_replica s WHERE s.subtotal <> s.total + s.discount_tier"
            " + s.discount_promo OR s.subtotal <> (SELECT COALESCE(sum(line_total), 0)"
            " FROM sale_line_replica l WHERE l.sale_id = s.sale_id)"
        )
        di3 = await central.fetchval(
            "SELECT count(*) FROM sale_replica s WHERE s.total <>"
            " (SELECT COALESCE(sum(amount), 0) FROM sale_payment_replica p"
            " WHERE p.sale_id = s.sale_id)"
        )
    finally:
        await central.close()
    assert (di1, di2, di3) == (0, 0, 0)
