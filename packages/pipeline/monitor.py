"""Bộ giám sát luồng — nguồn dữ liệu của dashboard "sức khỏe luồng" (docs/17 §6).

Metric tần suất (request/s, độ trễ, số sự kiện) do chính các tiến trình của luồng tự phát
(`shared.metrics`). Còn lại là metric TRẠNG THÁI, thứ chỉ đọc được từ nơi dữ liệu nằm: tuổi của
mép trích xuất (lake), mép "đã nạp tới" (nhật ký nạp ClickHouse), bao nhiêu đơn đã nạp mà dbt
chưa dựng, độ tươi của mart theo cửa hàng, số lệch INV-4 đang mở. Tiến trình này đọc chúng theo
chu kỳ rồi đẩy OTLP như mọi tiến trình khác.

Nằm ở `pipeline` vì nó đọc đúng ba nơi pipeline đã đọc (Postgres trung tâm, lake, ClickHouse),
bằng đúng các client đó, và không import code ứng dụng (hợp đồng `pipeline-is-standalone`). Chỉ
ĐỌC: không ghi gì, không giữ advisory lock của `run_once`, chạy song song với DAG thoải mái.

Chạy riêng, KHÔNG nằm trong DAG: DAG treo đúng là lúc các mép ngừng tiến, và một bộ đo chạy
bên trong DAG sẽ ngừng cùng lúc với thứ nó phải báo.

Mỗi nguồn đo độc lập. Nguồn chết thì `flow_monitor_source_up{source}` = 0 và các chỉ số của nó
vắng mặt (series ngừng) — không báo 0, vì 0 trông như "không trễ".

    uv run python -m pipeline health            # một lần, in JSON
    uv run python -m pipeline monitor --interval 30   # vòng lặp, đẩy OTLP
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncpg

from pipeline.clickhouse import quote
from pipeline.extract import parse_window
from pipeline.tables import INCREMENTAL

if TYPE_CHECKING:
    from pipeline.clickhouse import ClickHouse
    from pipeline.config import PipelineConfig
    from pipeline.lake import Lake

__all__ = ["METRICS", "FlowHealth", "Sample", "collect", "run_monitor"]

log = logging.getLogger(__name__)

#: Tên PROMETHEUS (sau khi bộ nhận OTLP thêm hậu tố) → mô tả. Một chỗ khai báo cho cả bộ
#: phát lẫn test kiểm dashboard (`tests/unit/test_flow_dashboard.py`): panel nào dùng tên không
#: có ở đây (hay ở các tiến trình khác) là panel mãi mãi trống.
METRICS: dict[str, str] = {
    # S3 — trung tâm nhận sự kiện
    "store_sync_lag_seconds": "recorded_at - occurred_at của lô gần nhất, theo cửa hàng",
    "store_last_seen_age_seconds": "Thời gian từ lần cuối cửa hàng liên lạc (lô hay heartbeat)",
    "dead_letter_events": "Sự kiện trung tâm đã vứt vào dead-letter, theo cửa hàng",
    "reconcile_drift_count": "Lệch INV-4 (số dư ≠ Σ sổ cái) đang mở — nghiêm trọng nhất",
    "reconcile_last_run_age_seconds": "Thời gian từ lần đối soát INV-4 cuối",
    "reconcile_full_last_run_age_seconds": "Thời gian từ lần QUÉT TOÀN BỘ INV-4 cuối",
    "point_ledger_partition_months_ahead": "Tháng partition point_ledger tạo sẵn (< 2 = nguy)",
    "pg_connections_used_ratio": "Kết nối client / max_connections của Postgres trung tâm",
    # S4 — trích xuất
    "pipeline_extract_horizon_lag_seconds": "now - extract_horizon(): lớn = transaction treo",
    "pipeline_extract_watermark_age_seconds": "now - mép cuối file bronze cuối, theo bảng",
    # S5 — nạp
    "pipeline_load_watermark_age_seconds": "now - mép cuối file đã nạp, theo bảng",
    "pipeline_loaded_until_age_seconds": "now - mép 'đã nạp tới' (min 7 bảng) mà dbt dùng",
    # S6 — biến đổi
    "pipeline_transform_pending_sales": "Đơn đã nạp dưới mép mà fact chưa có",
    "pipeline_transform_lag_seconds": "Tuổi đơn chờ dbt cũ nhất (0 = fact đã bắt kịp)",
    # Toàn luồng
    "flow_freshness_seconds": "now - max(occurred_at) theo tầng (L2, L4) và cửa hàng",
    # Chính bộ giám sát
    "flow_monitor_source_up": "1 = đọc được nguồn (central_pg, lake, clickhouse)",
    "flow_monitor_collect_duration_seconds": "Thời gian một lần đo",
}

#: Mart cũ hơn khoảng này không tính độ tươi: đọc giới hạn trong vài phân vùng tháng gần nhất
#: (fact phân vùng theo tháng của `occurred_at`) thay vì quét cả kho ở T2 (256 triệu dòng).
FRESHNESS_LOOKBACK_DAYS = 35

_RECONCILE_JOB = "point_balance_vs_ledger"  # central.ops.reconcile.JOB_NAME (không import được)
_RECONCILE_FULL_JOB = "point_balance_vs_ledger:full"  # central.ops.reconcile.FULL_JOB_NAME


@dataclass(frozen=True, slots=True)
class Sample:
    name: str
    value: float
    labels: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class FlowHealth:
    at: datetime
    samples: list[Sample] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, value: float, **labels: str) -> None:
        if name not in METRICS:  # chặn lệch giữa bộ phát và danh mục ngay khi viết
            raise KeyError(f"metric chưa khai trong METRICS: {name}")
        self.samples.append(Sample(name, float(value), tuple(sorted(labels.items()))))

    def value(self, name: str, **labels: str) -> float | None:
        want = tuple(sorted(labels.items()))
        return next((s.value for s in self.samples if s.name == name and s.labels == want), None)

    def age(self, ts: datetime) -> float:
        return max(0.0, (self.at - ts).total_seconds())

    def to_json(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "errors": self.errors,
            "metrics": [
                {"name": s.name, "labels": dict(s.labels), "value": s.value} for s in self.samples
            ],
        }


async def collect(
    cfg: PipelineConfig, lake: Lake, ch: ClickHouse, *, now: datetime | None = None
) -> FlowHealth:
    """Một lần đo mọi nguồn. Không bao giờ ném lỗi vì một nguồn chết."""
    health = FlowHealth(at=now or datetime.now(UTC))
    started = time.monotonic()
    for source, probe in (
        ("central_pg", _central(health, cfg)),
        ("lake", _to_async(_lake, health, lake)),
        ("clickhouse", _to_async(_warehouse, health, ch)),
    ):
        try:
            await probe
            health.add("flow_monitor_source_up", 1, source=source)
        except Exception as exc:
            health.errors[source] = f"{type(exc).__name__}: {str(exc)[:300]}"
            health.add("flow_monitor_source_up", 0, source=source)
    health.add("flow_monitor_collect_duration_seconds", time.monotonic() - started)
    return health


async def _to_async(fn: Any, *args: Any) -> None:
    fn(*args)


# ─────────────────────────────── Postgres trung tâm (S3, S4) ───────────────────────────────


async def _central(health: FlowHealth, cfg: PipelineConfig) -> None:
    conn = await asyncpg.connect(cfg.pg_dsn, timeout=10)
    try:
        for r in await conn.fetch(
            "SELECT store_id, lag_seconds, last_event_at, updated_at FROM store_sync_status"
        ):
            store = r["store_id"]
            if r["lag_seconds"] is not None:
                health.add("store_sync_lag_seconds", r["lag_seconds"], store_id=store)
            if r["last_event_at"] is not None:
                # Sự kiện mới nhất của cửa hàng đã tới trung tâm = độ tươi L2 (docs/17 §3).
                age = health.age(r["last_event_at"])
                health.add("flow_freshness_seconds", age, layer="L2", store_id=store)
            # `watched`: cửa hàng mà cảnh báo "im lặng" phải canh (docs/08 §5
            # `stores_not_seen_recently`). Mặc định mọi cửa hàng; stack dev chỉ định 3 cửa hàng
            # thật, vì cửa hàng ẢO của bộ giả lập im lặng là bình thường sau mỗi lần chạy.
            watched = "1" if not cfg.watch_stores or store in cfg.watch_stores else "0"
            health.add(
                "store_last_seen_age_seconds",
                health.age(r["updated_at"]),
                store_id=store,
                watched=watched,
            )

        for r in await conn.fetch(
            "SELECT COALESCE(store_id, 'unknown') AS store_id, count(*) AS n"
            " FROM dead_letter_event GROUP BY 1"
        ):
            health.add("dead_letter_events", r["n"], store_id=r["store_id"])

        drift = await conn.fetchval(
            "SELECT count(*) FROM reconciliation_drift WHERE resolved_at IS NULL"
        )
        health.add("reconcile_drift_count", drift)
        last_run = await conn.fetchval(
            "SELECT last_run_at FROM reconciliation_watermark WHERE job_name = $1", _RECONCILE_JOB
        )
        if last_run is not None:
            health.add("reconcile_last_run_age_seconds", health.age(last_run))
        last_full = await conn.fetchval(
            "SELECT last_run_at FROM reconciliation_watermark WHERE job_name = $1",
            _RECONCILE_FULL_JOB,
        )
        if last_full is not None:
            health.add("reconcile_full_last_run_age_seconds", health.age(last_full))

        # Ràng buộc #2: hết partition = mọi sự kiện có điểm bị từ chối. View chỉ đọc catalog.
        ahead = await conn.fetchval("SELECT months_ahead FROM point_ledger_partition_health")
        if ahead is not None:
            health.add("point_ledger_partition_months_ahead", ahead)

        # docs/08 §5 `pg_connections_used` > 70% → cần PgBouncer (docs/02 §4). Chỉ đếm kết nối
        # của client: tiến trình nền (autovacuum, WAL writer...) không chiếm `max_connections`.
        used = await conn.fetchval(
            "SELECT count(*)::float8 / current_setting('max_connections')::float8"
            " FROM pg_stat_activity WHERE backend_type = 'client backend'"
        )
        health.add("pg_connections_used_ratio", used)

        # Cùng hàm mà S4 dùng làm mép cửa sổ (docs/17 §4 bẫy 1). Trễ xa hơn `safety_lag` nghĩa
        # là có transaction mở lâu đang giữ trích xuất đứng lại — an toàn, nhưng phải thấy.
        horizon = await conn.fetchval(
            "SELECT extract_horizon(make_interval(secs => $1))", cfg.safety_lag_seconds
        )
        health.add("pipeline_extract_horizon_lag_seconds", health.age(horizon))
    finally:
        await conn.close()


# ─────────────────────────────────────── Lake (S4) ───────────────────────────────────────


def _lake(health: FlowHealth, lake: Lake) -> None:
    for spec in INCREMENTAL:
        ends = [w.end for key in lake.latest_files(spec.name) if (w := parse_window(key))]
        if ends:
            age = health.age(max(ends))
            health.add("pipeline_extract_watermark_age_seconds", age, table=spec.name)


# ─────────────────────────────────── ClickHouse (S5, S6, L4) ───────────────────────────────────


def _ts(value: str) -> datetime | None:
    if value in ("", "\\N"):
        return None
    return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=UTC)


def _ch_literal(ts: datetime) -> str:
    return f"toDateTime64({quote(ts.strftime('%Y-%m-%d %H:%M:%S.%f'))}, 6, 'UTC')"


def _warehouse(health: FlowHealth, ch: ClickHouse) -> None:
    names = [spec.name for spec in INCREMENTAL]
    loaded: dict[str, datetime] = {}
    for table, end in ch.rows(
        "SELECT table_name, toString(max(window_end)) FROM bronze_load_log"
        f" WHERE table_name IN ({', '.join(quote(n) for n in names)}) GROUP BY table_name"
    ):
        if (ts := _ts(end)) is not None:
            loaded[table] = ts
            health.add("pipeline_load_watermark_age_seconds", health.age(ts), table=table)
    # Cùng định nghĩa với macro dbt `loaded_until()`: đòi ĐỦ bảng, không thì chưa có mép.
    if len(loaded) < len(names):
        return
    until = min(loaded.values())
    health.add("pipeline_loaded_until_age_seconds", health.age(until))

    [[present]] = ch.rows(
        "SELECT count() FROM system.tables WHERE database = currentDatabase()"
        " AND name IN ('fact_sale_line', 'dim_store')"
    )
    if int(present) != 2:
        return  # dbt chưa chạy lần nào: chưa có gì để đo ở S6/L4

    [[built]] = ch.rows("SELECT toString(max(_recorded_at)) FROM fact_sale_line")
    since = _ts(built) or datetime(1970, 1, 1, tzinfo=UTC)
    # Đơn ĐÃ nạp (dưới mép) mà fact chưa có: đúng tập mà lượt dbt sau sẽ dựng (bẫy 5). Lọc
    # thêm `_dt` để ClickHouse chỉ mở các phân vùng tháng gần mép. `toDate32`, KHÔNG `toDate`:
    # fact rỗng → `since` = 1970-01-01, và `Date` trừ 7 ngày từ đó TRÀN thành năm 2149 → lọc mất
    # mọi đơn, báo "0 đơn chờ" đúng lúc dbt chưa từng chạy được (workflow `proof` 2026-09-28).
    [[pending, oldest]] = ch.rows(
        "SELECT uniqExact(sale_id), toString(min(recorded_at)) FROM bronze_sale"
        f" WHERE recorded_at > {_ch_literal(since)} AND recorded_at < {_ch_literal(until)}"
        f" AND _dt >= toDate32({_ch_literal(since)}) - 7"
    )
    health.add("pipeline_transform_pending_sales", int(pending))
    first = _ts(oldest) if int(pending) else None
    health.add("pipeline_transform_lag_seconds", health.age(first) if first else 0.0)

    for store, newest in ch.rows(
        "SELECT d.store_id, toString(max(f.newest)) FROM ("
        "  SELECT store_key, max(occurred_at) AS newest FROM fact_sale_line"
        f"  WHERE occurred_at >= now() - INTERVAL {FRESHNESS_LOOKBACK_DAYS} DAY"
        "  GROUP BY store_key"
        ") AS f INNER JOIN dim_store AS d ON d.store_key = f.store_key GROUP BY d.store_id"
    ):
        if (ts := _ts(newest)) is not None:
            health.add("flow_freshness_seconds", health.age(ts), layer="L4", store_id=store)


# ─────────────────────────────────── Đẩy OTLP theo chu kỳ ───────────────────────────────────


def _otel_name(prom: str) -> tuple[str, str]:
    """Tên Prometheus → (tên instrument OTel, đơn vị). Bộ nhận OTLP thêm lại `_seconds` từ `s`."""
    if prom.endswith("_seconds"):
        return prom.removesuffix("_seconds"), "s"
    return prom, "{item}"  # đơn vị dạng {...} không sinh hậu tố


def run_monitor(
    cfg: PipelineConfig,
    lake: Lake,
    ch: ClickHouse,
    *,
    interval_seconds: float,
    otlp_endpoint: str,
    service_name: str = "flow-monitor",
) -> None:  # pragma: no cover — vòng lặp tiến trình; phần đo được test qua `collect()`
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.metrics import Observation
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    latest: list[FlowHealth] = []
    stale_after = 3 * interval_seconds

    def observe(name: str) -> Any:
        def callback(_options: Any) -> list[Observation]:
            if not latest or (datetime.now(UTC) - latest[0].at).total_seconds() > stale_after:
                return []  # lần đo treo/chết: ngừng báo, không lặp lại số cũ
            snap = latest[0].samples
            return [Observation(s.value, dict(s.labels)) for s in snap if s.name == name]

        return callback

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics"),
        export_interval_millis=interval_seconds * 1000,
    )
    provider = MeterProvider(
        resource=Resource.create({"service.name": service_name}), metric_readers=[reader]
    )
    meter = provider.get_meter("pipeline.monitor")
    for prom, description in METRICS.items():
        name, unit = _otel_name(prom)
        meter.create_observable_gauge(
            name, callbacks=[observe(prom)], unit=unit, description=description
        )

    async def loop() -> None:
        while True:
            health = await collect(cfg, lake, ch)
            latest[:] = [health]
            if health.errors:
                log.warning("flow-monitor: nguồn lỗi %s", health.errors)
            await asyncio.sleep(interval_seconds)

    try:
        asyncio.run(loop())
    finally:
        provider.shutdown()
