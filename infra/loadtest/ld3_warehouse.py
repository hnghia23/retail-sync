"""LD-3 — dữ liệu T2 (24 tháng, ~256 triệu dòng hàng) trong ClickHouse, truy vấn lớp C < 3 giây.

docs/08 §6.3 + §4.4 ("ngưỡng ClickHouse"), docs/16 §4. Đi đúng đường thật từ S5 trở đi:

1. `simulator bulk` sinh lịch sử T2 thành Parquet bronze trên lake, dưới prefix RIÊNG
   (`bronze/bulk-t2` — không lẫn vào lake thật);
2. bộ nạp thật (`python -m pipeline load --until <cuối tháng>`) nạp TỪNG THÁNG vào database
   ClickHouse riêng (`dw_bulk`), sau mỗi tháng một lượt `dbt run` thật — như lịch production
   chạy suốt 24 tháng, và đo xem chi phí một lượt dbt có lớn dần theo lịch sử không;
3. `dbt test` một lần ở cuối; đối chiếu tổng của mart với đáp án của bộ sinh (`bulk.json`);
4. bộ truy vấn lớp C (B03 §2: toàn chuỗi, 12–24 tháng), mỗi câu 3 lần; dung lượng từng bảng.

    uv run python infra/loadtest/ld3_warehouse.py --months 24 --workers 6
    uv run python infra/loadtest/ld3_warehouse.py --skip-generate --months-limit 6 --label before

Báo cáo: `runs/load/<phiên>/LD-3[-label].json` (test `test_ld3_class_c_queries_under_3s` đọc nó).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "infra" / ".env"
CLICKHOUSE = "http://localhost:8123/"
DBT = [
    "uvx", "--python", "3.12", "--with-requirements", "data_platform/requirements-dbt.txt",
    "--from", "dbt-core", "dbt",
]  # fmt: skip

#: Lớp C (docs/business/03 §2): toàn chuỗi, 12–24 tháng, quét rộng, kết quả vài chục–nghìn dòng.
CLASS_C: dict[str, str] = {
    "revenue_by_month_24m": """
        SELECT toYYYYMM(business_date) AS m, sum(net_amount) AS revenue, uniqExact(sale_id) AS sales
        FROM fact_sale_line GROUP BY m ORDER BY m""",
    "rollup_day_x_store_24m": """
        SELECT date_key, store_key, sum(net_amount) AS revenue, uniqExact(sale_id) AS sales
        FROM fact_sale_line GROUP BY date_key, store_key""",
    "revenue_region_x_month": """
        SELECT s.region_id, toYYYYMM(f.business_date) AS m, sum(f.net_amount) AS revenue
        FROM fact_sale_line AS f INNER JOIN dim_store AS s ON s.store_key = f.store_key
        GROUP BY s.region_id, m ORDER BY s.region_id, m""",
    "top_products_24m": """
        SELECT product_key, sum(quantity) AS qty, sum(net_amount) AS revenue
        FROM fact_sale_line GROUP BY product_key ORDER BY revenue DESC LIMIT 50""",
    "store_ranking_12m_vs_prev": """
        SELECT store_key,
               sumIf(net_amount, business_date >= today() - 365) AS last_12m,
               sumIf(net_amount, business_date < today() - 365) AS prev_12m
        FROM fact_sale_line GROUP BY store_key ORDER BY last_12m DESC LIMIT 20""",
    "avg_ticket_by_month": """
        SELECT toYYYYMM(business_date) AS m, sum(net_amount) / uniqExact(sale_id) AS avg_ticket,
               count() / uniqExact(sale_id) AS lines_per_sale
        FROM fact_sale_line GROUP BY m ORDER BY m""",
    "payment_mix_by_month": """
        SELECT toYYYYMM(business_date) AS m, method, sum(amount) AS amount
        FROM fact_payment GROUP BY m, method ORDER BY m, method""",
    "cohort_join_x_active_month": """
        SELECT toYYYYMM(c.joined_at) AS cohort, toYYYYMM(f.business_date) AS active,
               uniqExact(f.customer_key) AS customers
        FROM fact_sale_line AS f INNER JOIN dim_customer AS c ON c.customer_key = f.customer_key
        WHERE f.customer_key != 0 AND c.is_inferred = 0
        GROUP BY cohort, active ORDER BY cohort, active""",
    "points_by_month_and_tier": """
        SELECT toYYYYMM(p.occurred_at) AS m, c.tier, sum(p.delta) AS points
        FROM fact_point_event AS p INNER JOIN dim_customer AS c ON c.customer_key = p.customer_key
        GROUP BY m, c.tier ORDER BY m, c.tier""",
    "market_basket_last_month_top200": """
        WITH last AS (SELECT max(toYYYYMM(business_date)) AS m FROM fact_sale_line),
        top AS (
            SELECT product_key FROM fact_sale_line
            WHERE toYYYYMM(business_date) = (SELECT m FROM last)
            GROUP BY product_key ORDER BY count() DESC LIMIT 200
        ),
        lines AS (
            SELECT sale_id, product_key FROM fact_sale_line
            WHERE toYYYYMM(business_date) = (SELECT m FROM last) AND product_key IN (SELECT * FROM top)
        )
        SELECT a.product_key AS p1, b.product_key AS p2, count() AS together
        FROM lines AS a INNER JOIN lines AS b ON a.sale_id = b.sale_id AND a.product_key < b.product_key
        GROUP BY p1, p2 ORDER BY together DESC LIMIT 50""",
}


def dotenv() -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


class CH:
    def __init__(self, user: str, password: str, database: str) -> None:
        self.headers = {"X-ClickHouse-User": user, "X-ClickHouse-Key": password}
        self.database = database
        self.client = httpx.Client(timeout=600)

    def query(self, sql: str, **settings: object) -> str:
        params = {"database": self.database} | {k: str(v) for k, v in settings.items()}
        r = self.client.post(CLICKHOUSE, params=params, headers=self.headers, content=sql.encode())
        if r.status_code != 200:
            raise RuntimeError(r.text[:1000])
        return r.text

    def rows(self, sql: str) -> list[list[str]]:
        return [line.split("\t") for line in self.query(f"{sql} FORMAT TSV").splitlines() if line]


def run(cmd: list[str], env: dict[str, str], *, check: bool = True) -> tuple[float, str]:
    started = time.perf_counter()
    r = subprocess.run(
        cmd, cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1", **env},
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    elapsed = time.perf_counter() - started
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:6])}… lỗi:\n{(r.stdout + r.stderr)[-3000:]}")
    return elapsed, r.stdout + r.stderr


def peak_memory(ch: CH, since: datetime) -> dict[str, Any]:
    ch.query("SYSTEM FLUSH LOGS")
    [[mem, rows, n]] = ch.rows(
        "SELECT max(memory_usage), sum(read_rows), count() FROM system.query_log"
        f" WHERE event_time >= toDateTime('{since:%Y-%m-%d %H:%M:%S}', 'UTC')"
        f" AND type = 'QueryFinish' AND current_database = '{ch.database}'"
    )
    return {
        "peak_memory_mb": round(int(mem or 0) / 2**20),
        "read_rows": int(rows or 0),
        "queries": int(n),
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="t2")
    parser.add_argument("--months", type=int, default=24)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--prefix", default="bronze/bulk-t2")
    parser.add_argument("--database", default="dw_bulk")
    parser.add_argument("--skip-generate", action="store_true")
    parser.add_argument("--months-limit", type=int, default=0, help="dừng sau N tháng (đo nhanh)")
    parser.add_argument("--fresh", action="store_true", help="DROP database trước khi nạp")
    parser.add_argument("--label", default="")
    parser.add_argument(
        "--dbt-dir",
        type=Path,
        default=ROOT / "data_platform" / "dbt",
        help="dự án dbt (đo bản cũ từ một git worktree: --dbt-dir ../wt/data_platform/dbt)",
    )
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)

    env = dotenv()
    user, password = env.get("CLICKHOUSE_USER", "dw"), env["CLICKHOUSE_PASSWORD"]
    ch = CH(user, password, args.database)
    session = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "runs" / "load" / session / f"LD-3{'-' + args.label if args.label else ''}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"profile": args.profile, "months": args.months, "label": args.label}

    def save() -> None:
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 1. Sinh lịch sử ──
    run_id = args.run_id or f"bulk-{args.profile}-{args.months}m-s42"
    if not args.skip_generate:
        seconds, log = run(
            [sys.executable, "-m", "simulator", "bulk", "--profile", args.profile,
             "--months", str(args.months), "--workers", str(args.workers),
             "--lake-prefix", args.prefix, "--run-id", run_id],
            {},
        )  # fmt: skip
        result["generate_seconds"] = round(seconds)
        print(log.strip().splitlines()[-2], flush=True)
    bulk = json.loads((ROOT / "runs" / run_id / "bulk.json").read_text(encoding="utf-8"))
    result["bulk"] = {k: bulk[k] for k in ("stores", "start_date", "end_date", "totals", "seconds")}
    save()

    pipe_env = {
        "PIPELINE_PG_DSN": (
            f"postgresql://{env.get('CENTRAL_DB_USER', 'central_app')}:"
            f"{env['CENTRAL_DB_PASSWORD']}@localhost:5434/central"
        ),
        "LAKE_ACCESS_KEY": env.get("MINIO_ROOT_USER", "minioadmin"),
        "LAKE_SECRET_KEY": env["MINIO_ROOT_PASSWORD"],
        "LAKE_PREFIX": args.prefix,
        "CLICKHOUSE_PASSWORD": password,
        "CLICKHOUSE_USER": user,
        "CLICKHOUSE_DB": args.database,
    }
    if args.fresh:
        httpx.post(
            CLICKHOUSE, headers=ch.headers, content=f"DROP DATABASE IF EXISTS {args.database}"
        )
    run([sys.executable, "-m", "pipeline", "init"], pipe_env)

    # ── 2. Từng tháng: nạp rồi dbt run ──
    months = sorted(bulk["published"])
    dbt_env = {
        "CLICKHOUSE_DB": args.database,
        "CLICKHOUSE_PASSWORD": password,
        "CLICKHOUSE_USER": user,
    }
    steps: list[dict[str, Any]] = []
    result["steps"] = steps
    for i, month in enumerate(months, start=1):
        if args.months_limit and i > args.months_limit:
            break
        end = f"{int(month[:4]) + int(month[4:]) // 12}-{int(month[4:]) % 12 + 1:02d}-01"
        load_s, _ = run([sys.executable, "-m", "pipeline", "load", "--until", end], pipe_env)
        since = datetime.now(UTC)
        step: dict[str, Any] = {"month": month, "load_seconds": round(load_s, 1)}
        try:
            dbt_s, log = run(
                [
                    *DBT,
                    "run",
                    "--project-dir",
                    str(args.dbt_dir),
                    "--profiles-dir",
                    str(args.dbt_dir),
                ],
                dbt_env,
            )
            step |= {"dbt_run_seconds": round(dbt_s, 1)} | peak_memory(ch, since)
        except RuntimeError as exc:
            step |= {"dbt_error": str(exc)[-1500:]} | peak_memory(ch, since)
            steps.append(step)
            save()
            print(f"  {month}: dbt LỖI — dừng. {str(exc)[-400:]}", flush=True)
            return 1
        [[fact_rows]] = ch.rows("SELECT count() FROM fact_sale_line")
        step["fact_sale_line_rows"] = int(fact_rows)
        steps.append(step)
        save()
        print(f"  {month}: nạp {load_s:.0f}s, dbt {dbt_s:.0f}s, đỉnh RAM {step['peak_memory_mb']} MB,"
              f" fact {int(fact_rows):,} dòng", flush=True)  # fmt: skip
    if args.months_limit and args.months_limit < len(months):
        return 0

    # ── 3. dbt test + đối chiếu đáp án ──
    # Hai chế độ: cửa sổ mặc định (lượt DAG mỗi giờ) và toàn bộ lịch sử (lịch định kỳ).
    result["dbt_test"] = {}
    for mode, extra in (("window", []), ("full", ["--vars", "{test_window_days: 36500}"])):
        since = datetime.now(UTC)
        test_s, log = run(
            [*DBT, "test", "--project-dir", str(args.dbt_dir), "--profiles-dir", str(args.dbt_dir),
             *extra],
            dbt_env, check=False,
        )  # fmt: skip
        result["dbt_test"][mode] = {
            "seconds": round(test_s, 1),
            "tail": log.strip().splitlines()[-3:],
        } | peak_memory(ch, since)
        save()
    [[sales, lines, revenue]] = ch.rows(
        "SELECT uniqExact(sale_id), count(), sum(net_amount) FROM fact_sale_line"
    )
    [[paid]] = ch.rows("SELECT sum(amount) FROM fact_payment")
    [[ledger]] = ch.rows("SELECT count() FROM fact_point_event")
    want = bulk["totals"]
    got = {"sales": int(sales), "lines": int(lines), "total_vnd": int(revenue),
           "paid_vnd": int(paid), "ledger": int(ledger)}  # fmt: skip
    result["totals"] = {"want": want, "got": got}
    result["totals_match"] = (
        got["sales"] == want["sales"]
        and got["lines"] == want["lines"]
        and got["total_vnd"] == want["total_vnd"] == got["paid_vnd"]
        and got["ledger"] == want["ledger"]
    )
    save()

    # ── 4. Truy vấn lớp C ──
    queries: dict[str, Any] = {}
    for name, sql in CLASS_C.items():
        times = []
        for _ in range(3):
            t = time.perf_counter()
            ch.query(f"{sql} FORMAT Null")
            times.append(time.perf_counter() - t)
        ch.query("SYSTEM FLUSH LOGS")
        [[read_rows, mem]] = ch.rows(
            "SELECT read_rows, memory_usage FROM system.query_log WHERE type = 'QueryFinish'"
            f" AND current_database = '{args.database}' AND query LIKE '%FORMAT Null%'"
            " ORDER BY event_time_microseconds DESC LIMIT 1"
        )
        queries[name] = {
            "seconds": [round(x, 3) for x in times],
            "median_seconds": round(statistics.median(times), 3),
            "max_seconds": round(max(times), 3),
            "read_rows": int(read_rows),
            "memory_mb": round(int(mem) / 2**20),
        }
        print(f"  {name}: {queries[name]['median_seconds']}s ({int(read_rows):,} dòng)", flush=True)
    result["queries"] = queries

    # ── 5. Dung lượng ──
    result["storage"] = {
        table: {
            "rows": int(r),
            "compressed_mb": round(int(c) / 2**20),
            "raw_mb": round(int(u) / 2**20),
        }
        for table, r, c, u in ch.rows(
            "SELECT table, sum(rows), sum(data_compressed_bytes), sum(data_uncompressed_bytes)"
            f" FROM system.parts WHERE active AND database = '{args.database}'"
            " GROUP BY table ORDER BY sum(data_compressed_bytes) DESC"
        )
    }
    save()
    print(f"báo cáo: {out}", flush=True)
    return (
        0 if result["totals_match"] and all(q["max_seconds"] < 3 for q in queries.values()) else 1
    )


if __name__ == "__main__":
    sys.exit(main())
