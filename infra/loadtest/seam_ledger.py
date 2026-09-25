"""Seam "phân vùng ledger": ~50 triệu dòng `point_ledger`, partition pruning chứng minh bằng EXPLAIN.

docs/08 §4.4 + cổng B ("50 triệu dòng ledger, partition pruning được chứng minh bằng `EXPLAIN`"),
docs/02 §4 ("> ~200 Tr dòng ledger → phân vùng theo tháng, đã thiết kế sẵn").

Dữ liệu là mảnh `point_ledger` + `customer` do `simulator bulk` sinh (cùng bộ sinh, cùng domain
tính điểm), `COPY` vào một database RIÊNG trên Postgres trung tâm (`central_seam`, dựng bằng
đúng migration Alembic thật — cùng partition, cùng 4 index, cùng khóa ngoại). DB thật không bị
đụng; `--keep` để giữ lại xem tay, mặc định xóa khi xong.

Mỗi truy vấn đo hai lần trên CÙNG dữ liệu: bình thường, và `SET enable_partition_pruning = off`
— A/B sạch, không cần bản sao không phân vùng.

    uv run python infra/loadtest/seam_ledger.py --work runs/bulk-t2-24m-s42/work \\
        --work runs/bulk-t2-extra-s43/work
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "infra" / ".env"
LEDGER_COLS = [
    "event_id", "customer_id", "store_id", "sale_id", "delta", "reason", "occurred_at",
    "recorded_at",
]  # fmt: skip


def dotenv() -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def shards(works: list[Path], table: str) -> dict[str, list[Path]]:
    by_month: dict[str, list[Path]] = {}
    for work in works:
        for path in sorted((work / table).glob("*/*.parquet")):
            by_month.setdefault(path.parent.name, []).append(path)
    return dict(sorted(by_month.items()))


async def copy_csv(conn: asyncpg.Connection, table: str, data: pa.Table, cols: list[str]) -> None:
    """Arrow → CSV (C++) → `COPY FROM STDIN`: không đi qua đối tượng Python từng dòng."""
    with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as fh:
        path = Path(fh.name)
    try:
        pacsv.write_csv(
            data.select(cols), path, write_options=pacsv.WriteOptions(include_header=False)
        )
        await conn.copy_to_table(table, source=str(path), columns=cols, format="csv")
    finally:
        path.unlink(missing_ok=True)


async def explain(conn: asyncpg.Connection, sql: str, *args: Any, pruning: bool) -> dict[str, Any]:
    await conn.execute(f"SET enable_partition_pruning = {'on' if pruning else 'off'}")
    plan = json.loads(await conn.fetchval(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", *args))[
        0
    ]
    scanned: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        if rel := node.get("Relation Name"):
            scanned.add(rel)
        for child in node.get("Plans", []):
            walk(child)

    walk(plan["Plan"])
    root = plan["Plan"]
    return {
        "execution_ms": round(plan["Execution Time"], 2),
        "planning_ms": round(plan["Planning Time"], 2),
        "partitions_scanned": len(scanned),
        "shared_hit_blocks": root.get("Shared Hit Blocks", 0),
        "shared_read_blocks": root.get("Shared Read Blocks", 0),
        "rows": root.get("Actual Rows"),
    }


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    env = dotenv()
    user = env.get("CENTRAL_DB_USER", "central_app")
    base = f"postgresql://{user}:{env['CENTRAL_DB_PASSWORD']}@localhost:5434"
    result: dict[str, Any] = {"database": args.database}

    admin = await asyncpg.connect(f"{base}/central")
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{args.database}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{args.database}"')
    finally:
        await admin.close()
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "packages/central/alembic.ini", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1",
             "DATABASE_URL": f"{base.replace('postgresql://', 'postgresql+asyncpg://')}/{args.database}"},
        check=True, capture_output=True, timeout=600,
    )  # fmt: skip

    ledger_shards = shards(args.work, "point_ledger")
    customer_shards = shards(args.work, "customer")
    first = min(ledger_shards)
    now = datetime.now(UTC)
    back = (now.year - int(first[:4])) * 12 + now.month - int(first[4:]) + 1
    conn = await asyncpg.connect(f"{base}/{args.database}", timeout=30)
    try:
        created = await conn.fetchval("SELECT ensure_point_ledger_partitions(3, $1)", back)
        result["partitions_created_back"] = {"months_back": back, "created": created}

        t0 = time.perf_counter()
        customers = 0
        for _month, paths in customer_shards.items():
            data = pa.concat_tables(pq.read_table(p) for p in paths)
            phone = pc.binary_join_element_wise("seam-", data.column("customer_id"), "")
            data = data.append_column("phone_hash", phone)
            await copy_csv(
                conn, "customer", data,
                ["customer_id", "phone_hash", "joined_at", "status", "recorded_at"],
            )  # fmt: skip
            customers += data.num_rows
        result["customers"] = {"rows": customers, "copy_seconds": round(time.perf_counter() - t0)}
        print(f"customer: {customers:,} dòng", flush=True)

        t0 = time.perf_counter()
        rows = 0
        per_month: dict[str, int] = {}
        for month, paths in ledger_shards.items():
            data = pa.concat_tables(pq.read_table(p) for p in paths)
            await copy_csv(conn, "point_ledger", data, LEDGER_COLS)
            rows += data.num_rows
            per_month[month] = data.num_rows
            print(f"  ledger {month}: {data.num_rows:,} (tổng {rows:,})", flush=True)
            if args.target and rows >= args.target:
                break
        copy_s = time.perf_counter() - t0
        result["ledger"] = {
            "rows": rows,
            "months": len(per_month),
            "copy_seconds": round(copy_s),
            "rows_per_second": round(rows / copy_s),
        }
        t0 = time.perf_counter()
        await conn.execute("ANALYZE point_ledger")
        await conn.execute("ANALYZE customer")
        result["ledger"]["analyze_seconds"] = round(time.perf_counter() - t0)
        result["size"] = dict(
            await conn.fetchrow(
                "SELECT pg_size_pretty(sum(pg_table_size(c.oid))) AS table_size,"
                " pg_size_pretty(sum(pg_indexes_size(c.oid))) AS index_size,"
                " count(*) AS partitions"
                " FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
                " WHERE i.inhparent = 'point_ledger'::regclass"
            )
        )

        # ── Truy vấn ──
        last_month = max(per_month)
        m_start = datetime(int(last_month[:4]), int(last_month[4:]), 1, tzinfo=UTC)
        m_end = (m_start + timedelta(days=32)).replace(day=1)
        heavy = await conn.fetchval(
            "SELECT customer_id FROM point_ledger WHERE occurred_at >= $1 AND occurred_at < $2"
            " GROUP BY customer_id ORDER BY count(*) DESC LIMIT 1",
            m_start, m_end,
        )  # fmt: skip
        sample = [
            r[0]
            for r in await conn.fetch(
                "SELECT customer_id FROM customer TABLESAMPLE SYSTEM (1) LIMIT 500"
            )
        ]
        recorded_hi = await conn.fetchval("SELECT max(recorded_at) FROM point_ledger")
        cases = {
            # Lớp A tại quầy: lịch sử điểm 90 ngày của khách đang đứng ở quầy.
            "customer_history_90d": (
                "SELECT event_id, delta, occurred_at FROM point_ledger"
                " WHERE customer_id = $1 AND occurred_at >= $2 ORDER BY occurred_at DESC",
                (heavy, m_end - timedelta(days=90)),
            ),
            # Lớp B/C: tổng điểm phát ra trong một tháng.
            "points_one_month": (
                "SELECT count(*), sum(delta) FROM point_ledger"
                " WHERE occurred_at >= $1 AND occurred_at < $2",
                (m_start, m_end),
            ),
            # Đối soát tăng dần (ràng buộc #8): khách có dòng mới trong 1 giờ ghi nhận cuối.
            "reconcile_changed_last_hour": (
                "SELECT DISTINCT customer_id FROM point_ledger"
                " WHERE recorded_at >= $1 AND recorded_at < $2",
                (recorded_hi - timedelta(hours=1), recorded_hi),
            ),
            # Đối soát: sổ cái của một lô 500 khách (`central.ops.reconcile._LEDGER`) — không có
            # điều kiện trên cột phân vùng nên KHÔNG prune được: đo để biết giá của quét toàn bộ.
            "reconcile_batch_500_customers": (
                "SELECT customer_id, delta, reason FROM point_ledger"
                " WHERE customer_id = ANY($1::uuid[]) ORDER BY customer_id, recorded_at, event_id",
                (sample,),
            ),
        }
        queries: dict[str, Any] = {}
        for name, (sql, qargs) in cases.items():
            on = await explain(conn, sql, *qargs, pruning=True)
            on = await explain(conn, sql, *qargs, pruning=True)  # lần 2: cache ấm, so công bằng
            off = await explain(conn, sql, *qargs, pruning=False)
            off = await explain(conn, sql, *qargs, pruning=False)
            queries[name] = {"pruning_on": on, "pruning_off": off}
            print(f"  {name}: on {on['execution_ms']} ms / {on['partitions_scanned']} partition,"
                  f" off {off['execution_ms']} ms / {off['partitions_scanned']}", flush=True)  # fmt: skip
        await conn.execute("SET enable_partition_pruning = on")
        result["queries"] = queries

        # Ước lượng quét toàn bộ INV-4 (lô 500 khách): số lô × thời gian một lô.
        batch_ms = queries["reconcile_batch_500_customers"]["pruning_on"]["execution_ms"]
        batches = -(-customers // 500)
        result["full_reconcile_estimate"] = {
            "customers": customers,
            "batches_of_500": batches,
            "ledger_query_ms_per_batch": batch_ms,
            "estimated_minutes_ledger_reads": round(batches * batch_ms / 60000, 1),
        }
    finally:
        await conn.close()
    if not args.keep:
        admin = await asyncpg.connect(f"{base}/central")
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{args.database}" WITH (FORCE)')
        finally:
            await admin.close()
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, action="append", required=True)
    parser.add_argument("--database", default="central_seam")
    parser.add_argument("--target", type=int, default=0, help="dừng COPY khi đủ N dòng (0 = hết)")
    parser.add_argument("--keep", action="store_true", help="giữ database sau khi đo")
    args = parser.parse_args(argv)
    result = asyncio.run(main_async(args))
    session = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "runs" / "load" / session / "SEAM-ledger.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"báo cáo: {out}")
    pruned = all(
        q["pruning_on"]["partitions_scanned"] < q["pruning_off"]["partitions_scanned"]
        for name, q in result["queries"].items()
        if name in ("customer_history_90d", "points_one_month")
    )
    return 0 if pruned and result["ledger"]["rows"] >= 50_000_000 else 1


if __name__ == "__main__":
    sys.exit(main())
