from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.config import OtelSettings, ReturnRules


class CentralSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "infra/.env"), env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://central_app:changeme-central@localhost:5434/central"

    # docs/13 §2 — hợp đồng timeout: server tự cắt ở 300ms, thấp hơn ngân sách 500ms của
    # client, để dư thời gian cho round-trip. Trả lỗi rõ ràng hơn là để client tự timeout.
    lookup_timeout_seconds: float = 0.3

    # Ràng buộc #2 — partition `point_ledger` phải tạo trước N tháng.
    # Hết partition = mọi INSERT lỗi = toàn hệ thống điểm chết (docs/08 §4.1).
    point_ledger_partitions_ahead_months: int = 3

    # Backpressure cho `POST /events` — docs/08 §3.2. Trung tâm TỪ CHỐI TỬ TẾ thay vì sập:
    # cửa hàng có outbox nên bị từ chối không mất gì, chỉ chậm hơn. Tình huống cần: 200 cửa
    # hàng cùng nối lại sau một sự cố mạng diện rộng (CH-6).
    ingest_max_batch: int = 500
    ingest_max_concurrency: int = 8
    ingest_retry_after_seconds: int = 5
    # Rate limit THEO CỬA HÀNG (token bucket, `429` + Retry-After) — một cửa hàng xả tồn đọng
    # không được giữ hết lượt của semaphore ở trên (`central.ingest.ratelimit`). 10 req/giây ×
    # lô 200 = 2000 sự kiện/giây/cửa hàng: không bao giờ chạm khi đồng bộ bình thường (một lô vài
    # giây một lần). 0 = tắt.
    ingest_store_rate_per_second: float = 10.0
    ingest_store_burst: int = 20

    # Quét TOÀN BỘ INV-4 (docs/08 §4.2 "mỗi tháng quét toàn bộ một lần vào giờ thấp điểm"):
    # `central-maintenance` chạy nó khi lần quét cuối cũ hơn N ngày VÀ giờ hiện tại (theo giờ cửa
    # hàng) nằm trong [start, end). Tới 2026-09-25 lệnh `--full` có nhưng không ai lên lịch.
    reconcile_full_every_days: float = 30.0
    reconcile_full_start_hour: int = 1
    reconcile_full_end_hour: int = 5
    # Giờ cửa hàng (VN, không có giờ mùa hè) — cùng giá trị với STORE_UTC_OFFSET_MINUTES ở edge.
    store_utc_offset_minutes: int = 420

    # Trần độ dài MỌI transaction của Central API (`transaction_timeout`, PostgreSQL 17+).
    # Trích xuất bronze lấy mép cửa sổ = transaction đang mở cũ nhất (`extract_horizon()`,
    # docs/17 §4 bẫy 1): một transaction treo không làm mất dữ liệu nhưng làm trích xuất đứng
    # lại. Trần này biến "đứng mãi" thành "đứng tối đa N giây". Một lô ingest 500 sự kiện
    # mất cỡ giây — 60 giây là dư hơn một bậc độ lớn. 0 = tắt.
    db_transaction_timeout_seconds: int = 60

    debug: bool = False


@lru_cache
def get_settings() -> CentralSettings:
    return CentralSettings()


@lru_cache
def get_return_rules() -> ReturnRules:
    return ReturnRules()


@lru_cache
def get_otel_settings() -> OtelSettings:
    return OtelSettings()
