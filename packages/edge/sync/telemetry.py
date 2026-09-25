"""Metric của sync worker — chặng S2 (docs/17 §6, docs/08 §5 `sync_failure_rate`).

Chỉ đếm kết quả mà `run_once()` đã trả (`BatchReport`), không đọc nội dung sự kiện: worker vẫn
là tầng vận chuyển (`import-linter`, hợp đồng `sync-is-transport-only`). Độ sâu outbox và tuổi
sự kiện cũ nhất KHÔNG ở đây mà ở Edge API (`edge.health.OutboxGauges`): worker chết thì chính
nó không báo được gì, trong khi đó mới là lúc outbox dồn.

Hai loại thất bại tách thành hai chỉ số, đúng như hai cách lùi trong `worker.py`:
  - `sync_push_failures_total` — lỗi của ĐƯỜNG TRUYỀN (cả lô không tới được trung tâm);
  - `sync_events_rejected_total{outcome=retry|dead_letter}` — trung tâm đã xét và từ chối.
Gộp hai thứ này thành một "tỉ lệ lỗi" là che mất đúng phân biệt mà AT-02 kiểm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shared.metrics import get_meter

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from edge.sync.worker import BatchReport


class SyncMetrics:
    def __init__(self, store_id: str, *, meter: Meter | None = None) -> None:
        meter = meter or get_meter("edge.sync")
        self._attrs = {"store_id": store_id}
        self._consecutive_failures = 0
        self.sent = meter.create_counter(
            "sync_events_sent", unit="{event}", description="Sự kiện trung tâm đã nhận"
        )
        self.rejected = meter.create_counter(
            "sync_events_rejected", unit="{event}", description="Sự kiện trung tâm từ chối"
        )
        self.push_failures = meter.create_counter(
            "sync_push_failures", unit="{batch}", description="Lô không tới được trung tâm"
        )
        self.batches = meter.create_counter(
            "sync_batches", unit="{batch}", description="Lô đã gửi và được trung tâm trả lời"
        )
        self.pruned = meter.create_counter(
            "sync_outbox_pruned", unit="{event}", description="Sự kiện đã gửi bị dọn khỏi outbox"
        )
        # Có series ngay từ lúc khởi động: worker sống mà chưa gửi gì là đường 0 trên dashboard,
        # không phải "No data" — thứ trông y hệt worker chết.
        for counter in (self.sent, self.push_failures, self.batches, self.pruned):
            counter.add(0, self._attrs)

        from opentelemetry.metrics import Observation

        def consecutive(_options: Any) -> list[Observation]:
            return [Observation(self._consecutive_failures, self._attrs)]

        meter.create_observable_gauge(
            "sync_consecutive_failures",
            callbacks=[consecutive],
            unit="{batch}",
            description="Số lần gửi lỗi đường truyền liên tiếp (0 = đang nối được trung tâm)",
        )

    def record_pruned(self, n: int) -> None:
        self.pruned.add(n, self._attrs)

    def record(self, report: BatchReport, *, consecutive_failures: int) -> None:
        self._consecutive_failures = consecutive_failures
        if report.transport_error:
            self.push_failures.add(1, self._attrs)
            return
        if not report.claimed:
            return
        self.batches.add(1, self._attrs)
        if report.sent:
            self.sent.add(report.sent, self._attrs)
        if report.retry_scheduled:
            self.rejected.add(report.retry_scheduled, self._attrs | {"outcome": "retry"})
        if report.dead_lettered:
            self.rejected.add(report.dead_lettered, self._attrs | {"outcome": "dead_letter"})
