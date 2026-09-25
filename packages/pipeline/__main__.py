"""CLI pipeline — S4 + S5.

    uv run python -m pipeline init   # tạo bucket + bảng bronze_* (chạy lại vô hại)
    uv run python -m pipeline run    # trích mọi cửa sổ đã an toàn → nạp mọi file chưa nạp
    uv run python -m pipeline load   # CHỈ S5: nạp mọi file chưa nạp (dữ liệu `simulator bulk`)
    uv run python -m pipeline health   # đo sức khỏe luồng MỘT lần, in JSON (docs/17 §6)
    uv run python -m pipeline monitor  # đo theo chu kỳ, đẩy OTLP cho dashboard (pipeline.monitor)

Cấu hình qua biến môi trường (`pipeline.config`): PIPELINE_PG_DSN, LAKE_ENDPOINT,
LAKE_ACCESS_KEY, LAKE_SECRET_KEY, LAKE_URL_FOR_CLICKHOUSE, CLICKHOUSE_URL, CLICKHOUSE_USER,
CLICKHOUSE_PASSWORD, CLICKHOUSE_DB (mặc định dw), LAKE_PREFIX (mặc định bronze/central),
PIPELINE_WINDOW_SECONDS (mặc định 3600). `monitor` cần thêm
OTEL_EXPORTER_OTLP_ENDPOINT; chu kỳ FLOW_MONITOR_INTERVAL_SECONDS (mặc định 30).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime

from pipeline.clickhouse import ClickHouse
from pipeline.config import PipelineConfig
from pipeline.lake import Lake
from pipeline.load import load_pending
from pipeline.monitor import collect, run_monitor
from pipeline.run import PipelineBusyError, run_once
from pipeline.tables import INCREMENTAL, SNAPSHOT

#: EX_TEMPFAIL — "thử lại sau", để lịch chạy phân biệt với lỗi thật.
EXIT_BUSY = 75


def main(argv: list[str] | None = None) -> int:
    # stdout UTF-8 (như shared.console.utf8_stdio — package này không import shared): trên
    # Windows, output bị chuyển hướng mã hóa bằng cp1252 và chết ở chữ tiếng Việt đầu tiên.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    parser.add_argument("command", choices=["init", "run", "load", "health", "monitor"])
    parser.add_argument(
        "--until",
        type=lambda v: datetime.fromisoformat(v).replace(tzinfo=UTC),
        default=None,
        help="load: chỉ nạp file có mép cuối cửa sổ ≤ mốc này (UTC, ví dụ 2025-01-01)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("FLOW_MONITOR_INTERVAL_SECONDS", "30")),
        help="monitor: chu kỳ đo (giây)",
    )
    args = parser.parse_args(argv)

    cfg = PipelineConfig.from_env()
    lake = Lake(cfg.lake)
    ch = ClickHouse(cfg.clickhouse)
    try:
        if args.command == "init":
            lake.ensure_bucket()
            ch.ensure_database()
            print(f"bucket={cfg.lake.bucket} bảng/lệnh DDL={ch.apply_ddl()}")
            return 0
        if args.command == "health":
            health = asyncio.run(collect(cfg, lake, ch))
            print(json.dumps(health.to_json(), ensure_ascii=False, indent=2))
            return 1 if health.errors else 0
        if args.command == "monitor":
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
            if not endpoint:
                raise SystemExit("Thiếu biến môi trường OTEL_EXPORTER_OTLP_ENDPOINT")
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            # Một dòng mỗi request, ~10 request mỗi 30 giây: nhiễu che mất cảnh báo nguồn lỗi.
            logging.getLogger("httpx").setLevel(logging.WARNING)
            run_monitor(cfg, lake, ch, interval_seconds=args.interval, otlp_endpoint=endpoint)
            return 0
        if args.command == "load":
            # Không trích xuất, không cần Postgres: nạp những gì đã nằm trên lake dưới LAKE_PREFIX
            # (lịch sử `simulator bulk`, LD-3). Cùng bộ nạp, cùng token khử trùng với `run`.
            loaded = {
                spec.name: sum(item.rows for item in load_pending(ch, lake, spec, until=args.until))
                for spec in (*INCREMENTAL, *SNAPSHOT)
            }
            print(json.dumps({"loaded_rows": loaded}, ensure_ascii=False, indent=2))
            return 0
        try:
            report = asyncio.run(run_once(cfg, lake, ch))
        except PipelineBusyError as exc:
            print(exc, file=sys.stderr)
            return EXIT_BUSY
        print(
            json.dumps(
                {"horizon": report.horizon.isoformat(), "tables": report.summary()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        ch.close()


if __name__ == "__main__":
    sys.exit(main())
