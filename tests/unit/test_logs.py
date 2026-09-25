"""Log JSON có cấu trúc (`shared.logs`) — docs/08 §5: JSON, `trace_id`, không log chuỗi tự do."""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest
from opentelemetry.sdk.trace import TracerProvider

from shared.logs import _MARK, formatter, setup_logging


def _record(msg: str, *args: object, exc: bool = False) -> logging.LogRecord:
    exc_info = None
    if exc:
        try:
            raise ValueError("hỏng")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
    return logging.LogRecord("edge.sync", logging.WARNING, __file__, 1, msg, args, exc_info)


def test_stdlib_record_becomes_one_json_line() -> None:
    line = formatter("sync-worker-store-001").format(_record("không gửi được (lần %d)", 3))
    event = json.loads(line)
    assert event["event"] == "không gửi được (lần 3)"  # tiếng Việt giữ nguyên, không \\u
    assert "\\u" not in line
    assert event["level"] == "warning"
    assert event["logger"] == "edge.sync"
    assert event["service"] == "sync-worker-store-001"
    assert event["timestamp"].endswith("Z")
    assert "trace_id" not in event  # ngoài span thì không bịa ra


def test_trace_id_of_the_current_span_is_attached() -> None:
    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("ingest.event") as span:
        event = json.loads(formatter("central-api").format(_record("từ chối")))
        ctx = span.get_span_context()
    assert event["trace_id"] == f"{ctx.trace_id:032x}"
    assert event["span_id"] == f"{ctx.span_id:016x}"


def test_exception_is_rendered_inside_the_json_line() -> None:
    event = json.loads(formatter("x").format(_record("lỗi cục bộ", exc=True)))
    assert "ValueError: hỏng" in event["exception"]


def test_setup_is_idempotent_and_keeps_foreign_handlers() -> None:
    root = logging.getLogger()
    foreign = logging.NullHandler()  # đóng vai handler của pytest (caplog)
    root.addHandler(foreign)
    try:
        setup_logging("a")
        setup_logging("b")
        ours = [h for h in root.handlers if getattr(h, _MARK, False)]
        assert len(ours) == 1  # gọi lại thay handler cũ, không nhân đôi dòng log
        assert foreign in root.handlers
        assert logging.getLogger("uvicorn.access").propagate is True
    finally:
        root.handlers[:] = [h for h in root.handlers if not getattr(h, _MARK, False)]
        root.removeHandler(foreign)


def test_handler_follows_stdout_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout bị thay SAU khi gắn handler (pytest bắt output, chuyển hướng) → vẫn ghi đúng chỗ.

    Bản đầu giữ stdout của lúc tạo handler: cuối suite, luồng export OTel ghi vào stdout pytest
    đã đóng và in traceback "--- Logging error ---"."""
    root = logging.getLogger()
    setup_logging("x")
    try:
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        logging.getLogger("t").warning("sau khi thay stdout")
        monkeypatch.undo()
        assert "sau khi thay stdout" in buf.getvalue()
    finally:
        root.handlers[:] = [h for h in root.handlers if not getattr(h, _MARK, False)]
