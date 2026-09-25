"""Metric OTel của luồng — `shared.metrics`, `edge.health.OutboxGauges`, `edge.sync.telemetry`.

Đọc số đo bằng `InMemoryMetricReader` trên một `MeterProvider` RIÊNG của test, không đụng tới
provider toàn cục (trạng thái toàn tiến trình, xem tests/unit/test_tracing.py).
"""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import shared.metrics as metrics_module
from edge.health import OutboxGauges, OutboxSnapshot
from edge.sync.telemetry import SyncMetrics
from edge.sync.worker import BatchReport
from shared.config import OtelSettings


def _reader() -> tuple[InMemoryMetricReader, MeterProvider]:
    reader = InMemoryMetricReader()
    return reader, MeterProvider(metric_readers=[reader])


def _points(reader: InMemoryMetricReader) -> dict[tuple[str, tuple[tuple[str, Any], ...]], Any]:
    out: dict[tuple[str, tuple[tuple[str, Any], ...]], Any] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for dp in m.data.data_points:
                    out[(m.name, tuple(sorted(dp.attributes.items())))] = dp.value
    return out


def test_setup_metrics_disabled_touches_nothing() -> None:
    before = metrics_module._metrics_initialized
    metrics_module.setup_metrics("x", settings=OtelSettings(otel_exporter_otlp_endpoint=None))
    assert metrics_module._metrics_initialized == before


# ═══════════════════════ OutboxGauges — S2, đo ở Edge API ═══════════════════════


def test_outbox_gauges_report_the_latest_snapshot_with_store_label() -> None:
    """`store_id` phải là ATTRIBUTE: Prometheus của otel-lgtm không nâng thuộc tính resource
    lên label (kiểm trên compose 2026-09-25), nên thiếu nó thì mọi cửa hàng chập làm một."""
    reader, provider = _reader()
    gauges = OutboxGauges("store-007", interval_seconds=15)
    gauges.register(provider.get_meter("t"))
    gauges.update(OutboxSnapshot(depth=12, oldest_unsent_age_seconds=340.5, dead_lettered=1))

    points = _points(reader)
    label = (("store_id", "store-007"),)
    assert points[("outbox_depth", label)] == 12
    assert points[("outbox_oldest_unsent_age", label)] == 340.5
    assert points[("outbox_dead_lettered", label)] == 1


def test_stale_outbox_snapshot_is_not_reported() -> None:
    """DB cửa hàng chết → không đọc được outbox → NGỪNG báo, không lặp mãi con số cũ trông
    như mọi thứ vẫn ổn."""
    gauges = OutboxGauges("store-007", interval_seconds=10)
    gauges.update(OutboxSnapshot(1, 5.0, 0), at=1000.0)
    assert gauges.current(now=1029.0) is not None
    assert gauges.current(now=1031.0) is None
    assert OutboxGauges("x", interval_seconds=10).current() is None  # chưa đo lần nào


# ═══════════════════════ SyncMetrics — S2, đo ở sync worker ═══════════════════════


def test_sync_metrics_keep_transport_failures_apart_from_rejections() -> None:
    """Hai loại thất bại của worker là hai chỉ số: gộp lại thì AT-02 (mất mạng không tính lượt
    thử) không còn nhìn thấy được trên dashboard."""
    reader, provider = _reader()
    m = SyncMetrics("store-001", meter=provider.get_meter("t"))

    m.record(BatchReport(claimed=200, transport_error="http 503"), consecutive_failures=1)
    m.record(BatchReport(claimed=200, transport_error="timeout"), consecutive_failures=2)
    m.record(
        BatchReport(claimed=10, sent=7, retry_scheduled=2, dead_lettered=1),
        consecutive_failures=0,
    )
    m.record(BatchReport(), consecutive_failures=0)  # vòng rỗng: không phải một lô

    s = (("store_id", "store-001"),)
    points = _points(reader)
    assert points[("sync_push_failures", s)] == 2
    assert points[("sync_batches", s)] == 1
    assert points[("sync_events_sent", s)] == 7
    assert points[("sync_events_rejected", (("outcome", "retry"), *s))] == 2
    assert points[("sync_events_rejected", (("outcome", "dead_letter"), *s))] == 1
    assert points[("sync_consecutive_failures", s)] == 0
