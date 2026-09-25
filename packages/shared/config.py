"""Cấu hình dùng chung — docs/05-data-model.md §5.

Phân biệt hai loại quy tắc nghiệp vụ, và đây CHỈ chứa loại thứ hai:

  1. Dữ liệu có hiệu lực theo thời gian (tỷ lệ tích điểm, ngưỡng hạng, khuyến mãi)
     → bảng trong DB trung tâm. Giao dịch cũ phải áp quy tắc cũ.
  2. Hành vi hệ thống (làm tròn, cộng gộp chiết khấu, số dư âm)
     → file này. Áp cho mọi giao dịch mới; đổi hồi tố sẽ sai.

⚠️ `rounding` và `allow_negative_balance` có hệ quả CHECK constraint (docs/05 §5) —
đổi giá trị sau khi có dữ liệu là một migration, không phải sửa biến môi trường.
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class _EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "infra/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


class PricingRules(_EnvSettings):
    """Q-B1..Q-B3. Prefix env: `PRICING_`."""

    model_config = SettingsConfigDict(env_prefix="PRICING_", extra="ignore")

    discount_stacking: Literal["additive", "multiplicative"] = "additive"
    # ⚠️ Hệ quả CHECK: làm tròn sai → CHECK (subtotal = total + discount) từ chối giao dịch,
    # không phải chỉ lệch số. Chốt MỘT giá trị từ migration đầu.
    rounding: Literal["to_dong_last_line", "per_line"] = "to_dong_last_line"
    promotion_applies_at: Literal["order_open", "order_close"] = "order_open"


class ReturnRules(_EnvSettings):
    """Q-B4..Q-B6. Prefix env: `RETURN_`."""

    model_config = SettingsConfigDict(env_prefix="RETURN_", extra="ignore")

    allow_cross_store: bool = False
    demote_tier_on_return: bool = False
    # ⚠️ Hệ quả CHECK: quyết định có `CHECK (balance >= 0)` trên `point_balance` hay không.
    allow_negative_balance: bool = True


class JwtSettings(_EnvSettings):
    model_config = SettingsConfigDict(env_prefix="JWT_", extra="ignore")

    # infra/.env.example) — không phải secret thật nằm trong code.
    secret_key: str = "changeme-dung-openssl-rand-hex-32"  # noqa: S105
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7


class PiiSettings(_EnvSettings):
    """Khóa băm SĐT — `shared.pii`. Prefix env: `PII_`.

    PHẢI giống nhau ở mọi cửa hàng và trung tâm: hash của cùng một SĐT phải so được với
    nhau để tra khách và dò trùng C03. Đổi khóa sau khi có dữ liệu = mọi hash cũ mất nghĩa.
    """

    model_config = SettingsConfigDict(env_prefix="PII_", extra="ignore")

    # Giá trị dev, cùng cách với `JwtSettings.secret_key` — production đặt qua biến môi trường.
    hash_key: str = "changeme-dev-pii-hash-key"


class SyncSettings(_EnvSettings):
    """Sync worker — ADR-003. Prefix env: `SYNC_`."""

    model_config = SettingsConfigDict(env_prefix="SYNC_", extra="ignore")

    batch_size: int = 200
    poll_interval_seconds: float = 2.0
    # Trần của backoff khi mất đường truyền. Jitter toàn phần (`next_backoff`) nên lần ngủ cuối
    # dài tối đa đúng bằng trần: có mạng lại thì worker thử lại muộn nhất sau chừng đó. NFR-04 /
    # CH-1 đòi hội tụ < 5 phút sau khi nối lại — 300 giây (bản đầu) chạm đúng mép, không còn chỗ
    # cho thời gian xả lô. 240 giây để lại 1 phút.
    backoff_max_seconds: float = 240.0
    # Rảnh bao lâu thì gửi một lô RỖNG làm heartbeat. Hai việc: trung tâm biết cửa hàng còn sống
    # dù không bán gì (`store_last_seen_age_seconds`), và worker phát hiện mất kết nối ngay cả khi
    # không có gì để gửi (`sync_consecutive_failures` — "circuit breaker" của docs/08 §5).
    heartbeat_seconds: float = 300.0
    max_attempts_before_dead_letter: int = 10
    # Trần của MỘT lần gửi lô. Worker giữ khóa hàng (`FOR UPDATE`) trên lô trong lúc chờ,
    # nên đây cũng là thời gian tối đa các dòng đó bị khóa.
    request_timeout_seconds: float = 10.0
    # Dọn outbox (ràng buộc #9): sự kiện ĐÃ GỬI quá N ngày bị xóa, theo lô, mỗi `prune_interval`.
    # Giữ vài ngày thay vì xóa ngay: trung tâm khôi phục từ backup cũ thì outbox là nguồn gửi bù.
    # Không bao giờ xóa sự kiện chưa gửi hay dead-letter (dead-letter chờ người xử lý).
    outbox_retention_days: float = 7.0
    prune_interval_seconds: float = 3600.0


class OtelSettings(_EnvSettings):
    """ADR-009. Tên biến theo chuẩn OTel nên không dùng prefix riêng."""

    model_config = SettingsConfigDict(extra="ignore")

    otel_service_name: str = "retail-sync"
    otel_exporter_otlp_endpoint: str | None = None
    otel_resource_attributes: str = ""
    otel_traces_sampler: str = "parentbased_traceidratio"
    otel_traces_sampler_arg: float = 1.0

    @property
    def enabled(self) -> bool:
        return bool(self.otel_exporter_otlp_endpoint)
