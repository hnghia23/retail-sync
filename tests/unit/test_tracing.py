"""OpenTelemetry — `shared/tracing.py`. ADR-009: Must, không phải "nếu kịp".

Không cần collector thật: SDK vẫn tạo span, propagate context, và export ở luồng nền dù
không nối được OTLP endpoint (lỗi export bị nuốt, log warning — không ảnh hưởng test).
Kiểm tra "trace thật sự tới được Grafana" là việc của spike hạ tầng
(docs/06-roadmap.md ngày 0, S5), không phải unit test.

⚠️ File này đụng vào TRẠNG THÁI TOÀN TIẾN TRÌNH (`opentelemetry.trace` global provider).
Test không được giả định thứ tự chạy, và không được để lại trạng thái khiến test khác của
suite sai lệch — dùng cổng `OTEL_EXPORTER_OTLP_ENDPOINT` trỏ về một địa chỉ vô hại
(`localhost:1`) để export luôn thất bại nhanh, không treo tiến trình test.
"""

from __future__ import annotations

import shared.tracing as tracing_module
from shared.config import OtelSettings
from shared.tracing import inject_trace, instrument_clients, link_from_trace, setup_tracing


def _enabled_settings(**overrides: object) -> OtelSettings:
    defaults: dict[str, object] = {
        "otel_exporter_otlp_endpoint": "http://localhost:1",  # cổng chết, export fail nhanh
        "otel_service_name": "test",
    }
    return OtelSettings(**(defaults | overrides))  # type: ignore[arg-type]


def _disabled_settings() -> OtelSettings:
    return OtelSettings(otel_exporter_otlp_endpoint=None)


# ═══════════════════════ setup_tracing — tắt ═══════════════════════


def test_disabled_settings_report_not_enabled() -> None:
    assert _disabled_settings().enabled is False


def test_setup_tracing_disabled_does_not_touch_global_flag() -> None:
    """Nhánh tắt phải return trước khi chạm `_tracing_initialized` — không được để lại
    dấu vết trạng thái."""
    was_initialized = tracing_module._tracing_initialized
    setup_tracing("service-khong-quan-trong", settings=_disabled_settings())
    assert tracing_module._tracing_initialized == was_initialized


# ═══════════════════════ setup_tracing — bật, idempotent ═══════════════════════


def test_setup_tracing_enabled_sets_provider_once_then_ignores_later_calls() -> None:
    """Hai khẳng định PHẢI nằm trong một test, không tách rời.

    Đây là trạng thái TOÀN TIẾN TRÌNH (`opentelemetry.trace` global provider) — một khi
    lần gọi đầu tiên trong tiến trình pytest đã set provider, mọi test khác trong cùng
    tiến trình đều thấy provider đó, bất kể thứ tự chạy. Tách thành hai test độc lập sẽ
    tạo phụ thuộc thứ tự ẩn: đúng cái bẫy mà cờ `_tracing_initialized` được thêm vào để
    ngăn — đã tự mắc phải khi viết bản đầu của file test này, sửa lại thành một test duy
    nhất kiểm cả "lần đầu có hiệu lực" lẫn "lần sau bị bỏ qua" trên cùng một chuỗi gọi.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    setup_tracing("dich-vu-thu-nhat", settings=_enabled_settings())
    provider_after_first = trace.get_tracer_provider()
    assert isinstance(provider_after_first, TracerProvider)
    assert provider_after_first.resource.attributes["service.name"] == "dich-vu-thu-nhat"

    setup_tracing("dich-vu-thu-hai", settings=_enabled_settings())
    assert trace.get_tracer_provider() is provider_after_first
    assert provider_after_first.resource.attributes["service.name"] == "dich-vu-thu-nhat"


def test_instrument_clients_can_be_called_twice_without_raising() -> None:
    """Mô phỏng đúng tình huống `create_app()` chạy lại trong cùng tiến trình
    (tests/integration/test_auth_http.py dựng lại app cho mỗi test)."""
    instrument_clients()
    instrument_clients()  # không được ném lỗi


# ═══════════════════════ inject_trace / link_from_trace ═══════════════════════


def test_inject_without_active_span_returns_empty_dict() -> None:
    setup_tracing("service-cho-inject-test", settings=_enabled_settings())
    from opentelemetry import trace

    # Đảm bảo không có span nào đang chạy tại thời điểm gọi.
    assert trace.get_current_span().get_span_context().is_valid is False
    assert inject_trace() == {}


def test_inject_inside_active_span_returns_valid_traceparent() -> None:
    setup_tracing("service-cho-inject-test-2", settings=_enabled_settings())
    from opentelemetry import trace

    tracer = trace.get_tracer("test-tracer")
    with tracer.start_as_current_span("mot-thao-tac"):
        carrier = inject_trace()

    assert "traceparent" in carrier
    # W3C traceparent: "00-<32 hex trace-id>-<16 hex span-id>-<2 hex flags>"
    parts = carrier["traceparent"].split("-")
    assert len(parts) == 4
    assert len(parts[1]) == 32
    assert len(parts[2]) == 16


def test_link_from_trace_recovers_span_context() -> None:
    """Đường đi thật: ghi outbox lúc có span → nhiều giờ sau, sync worker đọc `trace` từ
    payload → dựng span link (ràng buộc #7)."""
    setup_tracing("service-cho-link-test", settings=_enabled_settings())
    from opentelemetry import trace

    tracer = trace.get_tracer("test-tracer")
    with tracer.start_as_current_span("ghi-outbox") as span:
        expected_trace_id = span.get_span_context().trace_id
        carrier = inject_trace()

    links = link_from_trace(carrier)
    assert len(links) == 1
    assert links[0].context.trace_id == expected_trace_id
    assert links[0].context.is_valid


def test_link_from_trace_with_empty_carrier_returns_no_links() -> None:
    assert link_from_trace(None) == []
    assert link_from_trace({}) == []


def test_link_from_trace_with_garbage_carrier_returns_no_links() -> None:
    """Payload outbox cũ (`trace` rỗng vì tracing từng tắt) không được làm sync worker sập."""
    assert link_from_trace({"traceparent": "khong-phai-w3c-hop-le"}) == []
