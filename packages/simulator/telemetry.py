"""Kết quả đối soát → metric OTLP (`service.name=simulator`) — docs/17 §6 "chênh đối soát".

Chênh đối soát sau khi hội tụ là sự cố nghiêm trọng nhất của luồng, và chỉ bộ đối soát tính
được nó (nó giữ đáp án). Đẩy kết quả lên cùng chỗ với mọi chỉ số khác thì dashboard "sức khỏe
luồng" đặt được ô "chênh đối soát" cạnh độ trễ từng chặng, và test ngâm (`audit --watch`) để lại
một đường liên tục thay vì một đống `audit.json`.

    audit_status               0 CONVERGED · 1 CONVERGING · 2 DIVERGED
    audit_findings{level}      số phát hiện theo mức
    audit_freshness_seconds{layer, store_id}   now − đơn mới nhất của lần chạy ở tầng đó
    audit_behind_seconds{layer, store_id}      tụt sau tầng tươi nhất (0 = đã bắt kịp)

Một `AuditPublisher` cho cả vòng `--watch` (một `service.instance.id`, series liền mạch). Chạy
một lần thì đẩy một điểm rồi đóng — Prometheus giữ điểm đó 5 phút trên dashboard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from simulator.audit import AuditReport

STATUS_CODE = {"CONVERGED": 0, "CONVERGING": 1, "DIVERGED": 2}

#: Tên Prometheus → đơn vị OTel. `s` → bộ nhận OTLP thêm lại hậu tố `_seconds`.
_GAUGES = {
    "audit_status": "{status}",
    "audit_findings": "{finding}",
    "audit_freshness": "s",
    "audit_behind": "s",
}


def observations(report: AuditReport) -> dict[str, list[tuple[float, dict[str, str]]]]:
    """Bản thuần của phần đẩy: report → (giá trị, nhãn) cho từng gauge. Test không cần OTel."""
    out: dict[str, list[tuple[float, dict[str, str]]]] = {name: [] for name in _GAUGES}
    out["audit_status"].append((float(STATUS_CODE[report.status]), {}))
    for level in ("CONVERGING", "DIVERGED"):
        n = sum(1 for f in report.findings if f.level == level)
        out["audit_findings"].append((float(n), {"level": level}))
    for store_id, layers in report.freshness.items():
        for layer, f in layers.items():
            labels = {"layer": layer, "store_id": store_id}
            out["audit_freshness"].append((float(f["freshness_seconds"]), labels))
            out["audit_behind"].append((float(f["behind_seconds"]), labels))
    return out


class AuditPublisher:  # pragma: no cover — nối SDK; phần tính số ở `observations()` có test
    def __init__(self, endpoint: str, *, service_name: str = "simulator") -> None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.metrics import Observation
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        self._latest: dict[str, list[tuple[float, dict[str, str]]]] = {}
        # Chu kỳ rất dài: đẩy chủ động bằng `force_flush()` sau mỗi lần đối soát.
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{endpoint.rstrip('/')}/v1/metrics"),
            export_interval_millis=3_600_000,
        )
        self._provider = MeterProvider(
            resource=Resource.create({"service.name": service_name}), metric_readers=[reader]
        )
        meter = self._provider.get_meter("simulator.audit")

        def observe(name: str) -> Any:
            def callback(_options: Any) -> list[Observation]:
                return [Observation(v, labels) for v, labels in self._latest.get(name, [])]

            return callback

        for name, unit in _GAUGES.items():
            meter.create_observable_gauge(name, callbacks=[observe(name)], unit=unit)

    def publish(self, report: AuditReport) -> None:
        self._latest = observations(report)
        self._provider.force_flush()

    def close(self) -> None:
        self._provider.shutdown()
