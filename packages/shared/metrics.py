"""Metric OpenTelemetry — ADR-009, docs/17 §6, docs/08 §5.

Trace nói *vấn đề ở đâu*, metric nói *có vấn đề*. Dashboard "sức khỏe luồng" và ngưỡng cảnh
báo đọc metric, nên mỗi tiến trình của luồng (edge-api, sync worker, central-api) dựng MỘT
`MeterProvider` đẩy OTLP về cùng collector với trace.

Ba điều rút ra khi kiểm trên đúng `grafana/otel-lgtm` của compose (2026-09-25), không từ tài liệu:

1. **Thuộc tính resource KHÔNG thành label.** Prometheus của image chỉ nâng `service.name`,
   `service.instance.id`... lên label; `store.id` trong `OTEL_RESOURCE_ATTRIBUTES` chỉ nằm ở
   `target_info`. Metric nào cần tách theo cửa hàng thì mang `store_id` làm ATTRIBUTE của
   từng điểm đo.
2. **Bucket mặc định của histogram là 0…10000** (hợp cho mili-giây). Histogram đo bằng giây mà
   để mặc định thì mọi giá trị < 5 s rơi vào một bucket, p95 vô nghĩa. Vì vậy không tự tạo
   histogram giây ở đây: độ trễ request lấy từ instrumentation FastAPI theo semconv ỔN ĐỊNH
   (`http.server.request.duration`, giây, bucket 5 ms…10 s, label `http.route`), bật bằng
   `OTEL_SEMCONV_STABILITY_OPT_IN=http`. Semconv cũ ghi mili-giây và label `http.target`.
3. Tên đổi theo quy tắc của bộ nhận OTLP: đơn vị `s` → hậu tố `_seconds`, counter → `_total`,
   đơn vị dạng `{event}` không thêm gì. Đặt tên instrument theo đúng tên ở docs/08 §5 để
   Prometheus ra đúng tên đó (`outbox_oldest_unsent_age` + `s`
   → `outbox_oldest_unsent_age_seconds`).

Chu kỳ đẩy theo biến chuẩn `OTEL_METRIC_EXPORT_INTERVAL` (ms, SDK tự đọc; compose đặt 15 s để
test hỗn loạn thấy được diễn biến theo phút).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from shared.config import OtelSettings

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

# Cờ tiến trình — cùng lý do với `shared.tracing._tracing_initialized`: `set_meter_provider()`
# gọi lần hai bị bỏ qua ÂM THẦM, provider giữ resource của lần đầu.
_metrics_initialized = False


def setup_metrics(service_name: str, *, settings: OtelSettings | None = None) -> None:
    """Gắn MeterProvider cho tiến trình. No-op nếu chưa cấu hình OTLP endpoint.

    PHẢI gọi TRƯỚC `instrument_fastapi()`: instrumentation đọc lựa chọn semconv một lần lúc
    gắn vào app, và lấy meter từ provider toàn cục lúc đó.
    """
    global _metrics_initialized

    cfg = settings or OtelSettings()
    if not cfg.enabled or _metrics_initialized:
        return

    # Semconv HTTP ổn định (xem docstring module, điểm 2). `setdefault`: ai đặt tường minh thì
    # giữ nguyên lựa chọn của họ.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

    from opentelemetry import metrics
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{cfg.otel_exporter_otlp_endpoint}/v1/metrics")
    )
    provider = MeterProvider(
        resource=Resource.create({"service.name": service_name}), metric_readers=[reader]
    )
    metrics.set_meter_provider(provider)
    _metrics_initialized = True


def get_meter(name: str) -> Meter:
    """Meter của một thành phần. Tạo instrument được cả TRƯỚC `setup_metrics()`: API toàn cục
    trả meter "proxy", instrument tự nối vào provider thật khi nó được đặt — và là no-op nếu
    không bao giờ có provider (unit test, chạy không có collector)."""
    from opentelemetry import metrics

    return metrics.get_meter(name)
