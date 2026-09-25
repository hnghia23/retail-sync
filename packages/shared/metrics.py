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
from typing import TYPE_CHECKING, Any

from shared.config import OtelSettings

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.metrics import Meter
    from sqlalchemy.ext.asyncio import AsyncEngine

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


def register_resource_gauges(
    meter: Meter,
    *,
    engine: AsyncEngine,
    attrs: Mapping[str, str],
    disk_path: str | None = None,
) -> None:
    """Hai chỉ số tài nguyên của docs/08 §5, đọc ngay lúc export (rẻ, không chạm DB):

    - `db_pool_used_ratio` — kết nối pool đang cho mượn / (pool_size + max_overflow). Ngưỡng 80%:
      request tiếp theo sẽ phải CHỜ kết nối, độ trễ tăng trước khi có lỗi nào.
    - `disk_used_ratio` (khi có `disk_path`) — đĩa của máy cửa hàng. Ngưỡng 75%: "đầy đĩa" là
      rủi ro chí tử ở cửa hàng (docs/08 §3.1), và cảnh báo phải tới TRƯỚC lần ghi đầu tiên lỗi
      (CH-4). Đo đường dẫn nằm trên cùng đĩa với dữ liệu Postgres cửa hàng.
    """
    import shutil

    from opentelemetry.metrics import Observation

    from shared.db import MAX_OVERFLOW, POOL_SIZE

    labels = dict(attrs)
    pool = engine.sync_engine.pool

    def pool_used(_options: Any) -> list[Observation]:
        checked_out = pool.checkedout() if hasattr(pool, "checkedout") else 0
        return [Observation(checked_out / (POOL_SIZE + MAX_OVERFLOW), labels)]

    meter.create_observable_gauge(
        "db_pool_used_ratio",
        callbacks=[pool_used],
        unit="1",
        description="Tỉ lệ kết nối pool DB đang dùng",
    )
    if disk_path is None:
        return

    def disk_used(_options: Any) -> list[Observation]:
        try:
            usage = shutil.disk_usage(disk_path)
        except OSError:
            return []  # đường dẫn chưa mount: không báo gì, thay vì báo 0% trông như đĩa trống
        return [Observation(usage.used / usage.total, labels)] if usage.total else []

    meter.create_observable_gauge(
        "disk_used_ratio",
        callbacks=[disk_used],
        unit="1",
        description="Tỉ lệ dung lượng đĩa đã dùng",
    )


def get_meter(name: str) -> Meter:
    """Meter của một thành phần. Tạo instrument được cả TRƯỚC `setup_metrics()`: API toàn cục
    trả meter "proxy", instrument tự nối vào provider thật khi nó được đặt — và là no-op nếu
    không bao giờ có provider (unit test, chạy không có collector)."""
    from opentelemetry import metrics

    return metrics.get_meter(name)
