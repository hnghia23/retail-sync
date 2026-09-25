"""Dashboard "sức khỏe luồng" ↔ metric mà code thật sự phát (docs/17 §6).

Dashboard là JSON, không có trình biên dịch: gõ sai một tên metric (hoặc đổi tên ở code mà quên
dashboard) cho ra một panel TRỐNG mãi mãi, không lỗi nào. Trong test hỗn loạn, panel trống
trông y hệt "không có sự cố". Test này dựng danh mục tên từ CHÍNH code phát (instrument thật trên
một MeterProvider riêng, đổi tên theo đúng quy tắc của bộ nhận OTLP) rồi đòi mọi tên trong mọi
biểu thức PromQL của dashboard phải có trong danh mục.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from edge.health import OutboxGauges, OutboxSnapshot
from edge.sync.telemetry import SyncMetrics
from edge.sync.worker import BatchReport
from pipeline.monitor import METRICS as MONITOR_METRICS
from simulator.telemetry import _GAUGES as AUDIT_GAUGES

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "infra" / "observability" / "grafana" / "dashboards" / "flow-health.json"
INGEST = ROOT / "packages" / "central" / "ingest" / "telemetry.py"

#: Từ khóa/hàm PromQL — không phải tên metric.
_PROMQL = {
    "sum", "max", "min", "avg", "count", "by", "without", "rate", "irate", "increase", "clamp_min",
    "histogram_quantile", "or", "and", "unless", "vector", "le", "on", "ignoring",
}  # fmt: skip


def _prometheus_name(name: str, unit: str, *, counter: bool) -> str:
    """Quy tắc đổi tên của bộ nhận OTLP trong otel-lgtm 0.33.0 (kiểm thật 2026-09-25)."""
    out = name + ("_seconds" if unit == "s" else "")
    return out + ("_total" if counter else "")


def _emitted() -> set[str]:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("t")
    gauges = OutboxGauges("s", interval_seconds=15)
    gauges.register(meter)
    gauges.update(OutboxSnapshot(1, 1.0, 1))
    SyncMetrics("s", meter=meter).record(
        BatchReport(claimed=3, sent=1, retry_scheduled=1, dead_lettered=1), consecutive_failures=0
    )
    SyncMetrics("s2", meter=meter).record(BatchReport(transport_error="x"), consecutive_failures=1)

    names: set[str] = set()
    data = reader.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                counter = type(m.data).__name__ == "Sum"
                names.add(_prometheus_name(m.name, m.unit or "", counter=counter))

    # Counter mức module của ingest (meter proxy toàn cục): đọc tên từ mã nguồn.
    for name in re.findall(r'create_counter\(\s*"([a-z_]+)"', INGEST.read_text(encoding="utf-8")):
        names.add(f"{name}_total")
    names |= set(MONITOR_METRICS)
    names |= {_prometheus_name(n, u, counter=False) for n, u in AUDIT_GAUGES.items()}
    # Instrumentation FastAPI, semconv HTTP ổn định (shared.metrics).
    names |= {
        "http_server_request_duration_seconds_bucket",
        "http_server_request_duration_seconds_count",
    }
    return names


def _exprs() -> list[tuple[str, str]]:
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return [(p["title"], t["expr"]) for p in dash["panels"] for t in p.get("targets", [])]


def _metric_names(expr: str) -> set[str]:
    # Bỏ chuỗi TRƯỚC (`"edge-api-${store:regex}"` có `}` bên trong), rồi mới tới nhãn và khoảng.
    stripped = re.sub(r'"[^"]*"', " ", expr)
    stripped = re.sub(r"\{[^}]*\}|\[[^\]]*\]", " ", stripped)
    stripped = re.sub(r"\b\d+(\.\d+)?(e[-+]?\d+)?\b", " ", stripped)  # hằng số (1e-9, 0.95)
    return {w for w in re.findall(r"[a-z_][a-z0-9_]*", stripped) if w not in _PROMQL} - {
        # nhãn trong by (...) — không phải metric
        "job", "store_id", "table", "outcome", "reason", "level", "layer",
    }  # fmt: skip


def test_every_metric_on_the_dashboard_is_emitted_by_some_process() -> None:
    emitted = _emitted()
    missing = {(title, name) for title, expr in _exprs() for name in _metric_names(expr) - emitted}
    assert not missing, f"panel dùng metric không ai phát: {sorted(missing)}"


def test_dashboard_covers_every_stage_of_the_flow() -> None:
    """docs/17 §6 gom chỉ số "theo chiều trái → phải": thiếu một chặng là mù đúng chặng đó."""
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    rows = [p["title"] for p in dash["panels"] if p["type"] == "row"]
    for stage in ("S1", "S2", "S3", "S4", "S6", "Toàn luồng"):
        assert any(stage in r for r in rows), stage
    names = {n for _, expr in _exprs() for n in _metric_names(expr)}
    # Những chỉ số docs/08 §5 gọi tên là cảnh báo — phải nhìn thấy được.
    for must in (
        "outbox_oldest_unsent_age_seconds",
        "store_sync_lag_seconds",
        "reconcile_drift_count",
        "dead_letter_events",
        "flow_freshness_seconds",
        "audit_status",
    ):
        assert must in names, must


def test_panel_ids_are_unique_and_the_grid_has_no_overlap() -> None:
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids))
    cells: set[tuple[int, int]] = set()
    for p in dash["panels"]:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24, p["title"]
        box = {
            (x, y) for x in range(g["x"], g["x"] + g["w"]) for y in range(g["y"], g["y"] + g["h"])
        }
        assert not box & cells, f"chồng lên nhau: {p['title']}"
        cells |= box


ALERTS = (
    ROOT / "infra" / "observability" / "grafana" / "provisioning" / "alerting" / "retail-sync.yaml"
)


def _alert_rules() -> list[dict[str, object]]:
    # File là JSON (tập con của YAML) sau các dòng chú thích `#`.
    body = "\n".join(
        line for line in ALERTS.read_text(encoding="utf-8").splitlines() if not line.startswith("#")
    )
    rules: list[dict[str, object]] = json.loads(body)["groups"][0]["rules"]
    return rules


def test_every_alert_uses_a_metric_that_some_process_emits() -> None:
    """Cảnh báo trên metric không ai phát thì không bao giờ kêu — tệ hơn panel trống, vì không ai
    nhìn cảnh báo cho tới lúc nó cần kêu."""
    emitted = _emitted()
    for rule in _alert_rules():
        data = rule["data"]
        assert isinstance(data, list)
        expr = data[0]["model"]["expr"]
        assert not _metric_names(expr) - emitted, (rule["title"], _metric_names(expr) - emitted)


def test_alerts_cover_the_severe_signals_of_docs_08() -> None:
    names = {n for r in _alert_rules() for n in _metric_names(r["data"][0]["model"]["expr"])}  # type: ignore[index]
    for must in (
        "reconcile_drift_count",
        "point_ledger_partition_months_ahead",
        "outbox_oldest_unsent_age_seconds",
        "dead_letter_events",
        "store_sync_lag_seconds",
    ):
        assert must in names, must
    uids = [r["uid"] for r in _alert_rules()]
    assert len(uids) == len(set(uids))
