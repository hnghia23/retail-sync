"""Metric của `POST /events` — chặng S3 (docs/17 §6, docs/08 §5).

- `ingest_events_total{store_id, outcome}`: `event_duplicate_rate` = duplicate / tổng, tỉ lệ
  `rejected_*`. Ghi SAU commit (xem `ingest_batch`).
- `ingest_batches_refused_total{reason}`: lô bị từ chối nguyên cả lô — `overloaded` (`503`,
  semaphore), `rate_limited` (`429`, một cửa hàng gửi quá nhanh) hoặc `too_large` (`413`). Thứ
  CH-6 (10 cửa hàng nối lại cùng lúc) phải thấy tăng rồi về 0.

Độ trễ của request không ở đây: instrumentation FastAPI đã đo
(`http_server_request_duration_seconds{http_route="/api/v1/events"}`, xem `shared.metrics`).

Nhãn `store_id` là tập đóng (cửa hàng đã cấp khóa), không phải đầu vào tự do: request chưa qua
xác thực không bao giờ tới chỗ đếm theo cửa hàng.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shared.metrics import get_meter

if TYPE_CHECKING:
    from central.ingest.service import IngestStats

_meter = get_meter("central.ingest")
_events = _meter.create_counter(
    "ingest_events", unit="{event}", description="Sự kiện đã xét ở trung tâm, theo kết cục"
)
_refused = _meter.create_counter(
    "ingest_batches_refused", unit="{batch}", description="Lô bị từ chối nguyên cả lô"
)


def record_batch(store_id: str, stats: IngestStats) -> None:
    for outcome, n in stats.outcomes().items():
        if n:
            _events.add(n, {"store_id": store_id, "outcome": outcome})


def record_refused(reason: str) -> None:
    _refused.add(1, {"reason": reason})
