"""Rate limit theo cửa hàng cho `POST /events` — docs/08 §3.2, CH-6.

Hai cơ chế backpressure khác nhau, chặn hai chuyện khác nhau:

- `INGEST_MAX_CONCURRENCY` (semaphore, `503`): trung tâm quá tải NÓI CHUNG — bảo vệ Postgres.
- Rate limit theo cửa hàng (token bucket, `429`): MỘT cửa hàng chiếm hết lượt. Sau sự cố mạng
  diện rộng, cửa hàng có 3 ngày tồn đọng xả lô liên tiếp không nghỉ (lô đầy → gửi tiếp ngay,
  `edge.sync.worker`); không có giới hạn này thì nó giữ semaphore liên tục và các cửa hàng khác
  chỉ nhận `503`.

Cả hai đều là lỗi của ĐƯỜNG TRUYỀN với worker: không đốt lượt thử của sự kiện nào, worker ngủ
đúng `Retry-After` (CLAUDE.md, quy tắc của sync worker).

Trạng thái nằm trong bộ nhớ của MỘT tiến trình. Chạy N bản Central API thì giới hạn thực là
N × `rate` — chấp nhận được (mục tiêu là chia lượt, không phải hạn ngạch chính xác). Scale seam:
chuyển bucket sang Redis khi cần giới hạn chính xác trên nhiều bản.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["StoreRateLimiter"]


@dataclass(slots=True)
class _Bucket:
    tokens: float
    at: float


class StoreRateLimiter:
    """Token bucket mỗi cửa hàng: `rate` request/giây, dồn tối đa `burst` request.

    `rate <= 0` tắt giới hạn (mọi request đều qua)."""

    def __init__(
        self, *, rate: float, burst: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.rate = rate
        self.burst = max(1, burst)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def acquire(self, store_id: str) -> float | None:
        """Lấy một lượt. `None` = được qua; ngược lại là số giây phải chờ (cho `Retry-After`).

        Không có `await` bên trong: kiểm-rồi-trừ nguyên tử trong asyncio."""
        if self.rate <= 0:
            return None
        now = self._clock()
        bucket = self._buckets.get(store_id)
        if bucket is None:
            bucket = self._buckets[store_id] = _Bucket(float(self.burst), now)
        bucket.tokens = min(float(self.burst), bucket.tokens + (now - bucket.at) * self.rate)
        bucket.at = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return None
        return (1 - bucket.tokens) / self.rate

    @staticmethod
    def retry_after_header(wait_seconds: float) -> str:
        """`Retry-After` là số nguyên giây (RFC 9110); làm tròn LÊN, tối thiểu 1."""
        return str(max(1, math.ceil(wait_seconds)))
