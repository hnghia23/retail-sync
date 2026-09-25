"""Log JSON có cấu trúc — docs/08 §5, docs/07 (structlog → JSON + `trace_id`).

Code ghi log bằng `logging.getLogger(__name__)` như cũ (stdlib), và mọi thư viện (uvicorn,
SQLAlchemy, OTel exporter) cũng vậy. `setup_logging()` gắn MỘT handler lên root logger với
`structlog.stdlib.ProcessorFormatter`, nên TẤT CẢ các dòng log ra stdout cùng một dạng JSON, kể
cả dòng của thư viện — không phải chỉ những chỗ code gọi structlog.

Mỗi dòng mang `trace_id`/`span_id` của span OTel đang chạy (nếu có): từ một dòng log lỗi ở trung
tâm tra ngược được trace, và qua span link về tận giao dịch gốc ở cửa hàng (docs/10 §4).

Ràng buộc #10 không tự cưỡng chế được ở đây: formatter không biết chuỗi nào là SĐT. Quy tắc là
ở chỗ GỌI log — chỉ `customer_id`, không bao giờ tên hay SĐT.

`LOG_FORMAT=text` cho người đọc ở terminal khi phát triển; mặc định `json` (container).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

__all__ = ["setup_logging"]

#: Logger của uvicorn tự gắn handler riêng (định dạng chữ). Gỡ đi và cho đổ về root, để mọi dòng
#: — cả access log — cùng một định dạng.
_OWN_HANDLERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_MARK = "_retail_sync_json"


def _add_trace(_logger: Any, _method: str, event: MutableMapping[str, Any]) -> Mapping[str, Any]:
    try:
        from opentelemetry.trace import get_current_span

        ctx = get_current_span().get_span_context()
    except Exception:  # pragma: no cover — log không bao giờ được chết vì tracing
        return event
    if ctx.is_valid:
        event["trace_id"] = f"{ctx.trace_id:032x}"
        event["span_id"] = f"{ctx.span_id:016x}"
    return event


def formatter(service: str, *, fmt: str = "json") -> logging.Formatter:
    """Formatter cho handler stdlib. Tách riêng để test không phải đụng root logger."""

    def add_service(
        _logger: Any, _method: str, event: MutableMapping[str, Any]
    ) -> Mapping[str, Any]:
        event.setdefault("service", service)
        return event

    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=False)
        if fmt == "text"
        else structlog.processors.JSONRenderer(ensure_ascii=False)
    )
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            add_service,
            _add_trace,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Ghi vào `sys.stdout` CỦA LÚC GHI, không phải của lúc tạo handler.

    StreamHandler thường giữ đối tượng stream được đưa lúc khởi tạo. Khi stdout bị thay (pytest bắt
    output rồi đóng nó cuối test), luồng export OTel ghi log sau đó gặp "I/O operation on closed
    file" và in cả traceback "--- Logging error ---" (thấy ở lần chạy suite 2026-09-25)."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


def setup_logging(service: str, *, level: str | None = None) -> None:
    """Gắn handler JSON lên root. Gọi lại được (thay handler cũ, không nhân đôi dòng log)."""
    handler = _StdoutHandler()
    handler.setFormatter(formatter(service, fmt=os.environ.get("LOG_FORMAT", "json")))
    setattr(handler, _MARK, True)
    root = logging.getLogger()
    # Chỉ thay handler do CHÍNH hàm này gắn lần trước: `create_app()` chạy lại nhiều lần trong
    # một tiến trình (test), và gỡ hết handler của root là gỡ luôn handler bắt log của pytest.
    root.handlers[:] = [h for h in root.handlers if not getattr(h, _MARK, False)] + [handler]
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    for name in _OWN_HANDLERS:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
