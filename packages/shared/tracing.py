"""OpenTelemetry — ADR-009 + docs/10-observability.md.

Hai việc, và chỉ hai việc:
  1. `setup_tracing()` — dựng provider + OTLP exporter, gắn auto-instrumentation.
  2. `inject_trace()` / `link_from_trace()` — mang trace context QUA outbox.

Về (2): độ trễ giữa lúc ghi outbox và lúc worker gửi có thể hàng giờ. Nối bằng
parent-child sẽ tạo span dài vô nghĩa, nên đầu nhận dùng **span link** (ràng buộc #7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shared.config import OtelSettings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.trace import Link

# ⚠️ Cờ tiến trình, KHÔNG phải per-app. `create_app()` có thể chạy nhiều lần trong cùng
# tiến trình (dev server auto-reload, hoặc test tự dựng lại app — xem
# tests/integration/test_auth_http.py). OpenTelemetry cho `set_tracer_provider()` gọi lại
# nhiều lần nhưng chỉ LẦN ĐẦU có hiệu lực; các lần sau bị bỏ qua ÂM THẦM (chỉ log warning)
# và provider giữ nguyên resource của lần gọi đầu tiên. Không cờ chặn tường minh thì lần
# gọi thứ hai với `service_name` khác sẽ ghi trace dưới tên SAI mà không có lỗi nào cảnh
# báo — đã xác minh hành vi này bằng thực nghiệm trước khi viết đoạn code này.
_tracing_initialized = False
_clients_instrumented = False


def _sampler(cfg: OtelSettings) -> Any:
    """Bộ lấy mẫu từ `OtelSettings` (cùng tên biến chuẩn OTel). Trước 2026-10-08 hai field này
    khai báo mà không ai dùng — SDK tự đọc biến môi trường, nên mặc định trong code là chữ chết.
    `parentbased_*`: request có `traceparent` đã lấy mẫu ở đầu kia thì đi theo quyết định đó."""
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )

    ratio = TraceIdRatioBased(cfg.otel_traces_sampler_arg)
    return {
        "always_on": ALWAYS_ON,
        "always_off": ALWAYS_OFF,
        "traceidratio": ratio,
        "parentbased_always_on": ParentBased(ALWAYS_ON),
        "parentbased_always_off": ParentBased(ALWAYS_OFF),
        "parentbased_traceidratio": ParentBased(ratio),
    }[cfg.otel_traces_sampler]


def setup_tracing(service_name: str, *, settings: OtelSettings | None = None) -> None:
    """Gắn tracing cho tiến trình hiện tại. No-op nếu chưa cấu hình OTLP endpoint.

    No-op có chủ đích: unit test và `pytest` chạy không cần collector, nhưng code đường
    chính không phải rẽ nhánh `if tracing_enabled`.
    """
    global _tracing_initialized

    cfg = settings or OtelSettings()
    if not cfg.enabled:
        return
    if _tracing_initialized:
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from shared.metrics import resource_attributes

    resource = Resource.create(resource_attributes(service_name))
    provider = TracerProvider(resource=resource, sampler=_sampler(cfg))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{cfg.otel_exporter_otlp_endpoint}/v1/traces")
        )
    )
    trace.set_tracer_provider(provider)
    _tracing_initialized = True


def instrument_fastapi(app: FastAPI) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)


def instrument_clients() -> None:
    """Auto-instrument asyncpg / redis / httpx. Gọi một lần lúc khởi động.

    Idempotent qua cờ tiến trình — cùng lý do với `setup_tracing()`. Các lớp
    `*Instrumentor` của OTel tự chịu được gọi `.instrument()` nhiều lần (chỉ log warning,
    không ném lỗi), nhưng cờ tường minh ở đây tránh việc log rác mỗi lần `create_app()`
    chạy lại.
    """
    global _clients_instrumented
    if _clients_instrumented:
        return

    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    AsyncPGInstrumentor().instrument()
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    _clients_instrumented = True


def inject_trace() -> dict[str, str]:
    """Trả về `{"traceparent": ...}` của span đang chạy, để nhét vào payload outbox.

    Trả dict rỗng nếu chưa có tracing — không được ném lỗi làm hỏng việc bán hàng.
    """
    carrier: dict[str, str] = {}
    try:
        from opentelemetry.propagate import inject

        inject(carrier)
    except Exception:  # pragma: no cover — tracing không bao giờ được chặn đường chính
        return {}
    return carrier


def link_from_trace(carrier: dict[str, str] | None) -> list[Link]:
    """Dựng span link từ trace context đã lưu trong outbox (ràng buộc #7).

    Dùng ở sync worker và ở central ingest:
        with tracer.start_as_current_span("ingest", links=link_from_trace(ev.trace)):
    """
    if not carrier:
        return []
    try:
        from opentelemetry.propagate import extract
        from opentelemetry.trace import Link, get_current_span

        ctx = extract(carrier)
        span_ctx = get_current_span(ctx).get_span_context()
    except Exception:  # pragma: no cover
        return []
    if not span_ctx.is_valid:
        return []
    return [Link(span_ctx)]
