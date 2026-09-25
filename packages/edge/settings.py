"""Cấu hình Edge API. Biến môi trường do infra/compose.yaml cấp."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.config import (
    JwtSettings,
    OtelSettings,
    PiiSettings,
    PricingRules,
    ReturnRules,
    SyncSettings,
)


class EdgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "infra/.env"), env_file_encoding="utf-8", extra="ignore"
    )

    store_id: str = "store-001"
    database_url: str = "postgresql+asyncpg://edge_app:changeme-edge@localhost:5433/edge_store_001"
    redis_url: str = "redis://localhost:6380/0"
    central_api_url: str = "http://localhost:8000"
    # Khóa của CỬA HÀNG NÀY do trung tâm cấp (`central.ops.provision_store`). Rỗng = chưa
    # đăng ký: sync worker từ chối khởi động thay vì nhận `401` mãi mãi (docs/13 §2).
    central_api_key: str = ""

    # docs/13 §4 — ngân sách tra cứu khách ở client. Vượt thì coi như khách ẩn danh,
    # KHÔNG chặn bán hàng (nguyên tắc kiến trúc #1).
    central_lookup_timeout_seconds: float = 0.5
    customer_cache_ttl_seconds: int = 900  # 15 phút

    # Cảnh báo master data cũ — B02 E03
    master_data_stale_after_hours: int = 24

    # Múi giờ của cửa hàng, dạng độ lệch so với UTC (phút). Việt Nam không có giờ mùa hè
    # nên một độ lệch cố định là đủ, và không phụ thuộc gói `tzdata` (Windows không có sẵn).
    # Dùng để kiểm `business_date` lúc mở ca — không dùng để SUY RA nó (G5).
    store_utc_offset_minutes: int = 420

    # Chu kỳ đọc outbox cho metric S2 (`edge.health.OutboxGauges`). Hai câu lệnh trên partial
    # index mỗi lần — rẻ, nhưng không cần dày hơn chu kỳ đẩy metric (15 s trong compose).
    outbox_metrics_interval_seconds: float = 15.0

    # Đĩa của máy cửa hàng cho metric `disk_used_ratio` (docs/08 §5, CH-4). Phải nằm trên cùng
    # đĩa với dữ liệu Postgres cửa hàng: ở một máy cửa hàng thật đó là `/`; trong compose mọi
    # container và named volume cũng chung một đĩa của máy ảo Docker.
    disk_path: str = "/"

    debug: bool = False


@lru_cache
def get_settings() -> EdgeSettings:
    return EdgeSettings()


@lru_cache
def get_pricing_rules() -> PricingRules:
    return PricingRules()


@lru_cache
def get_return_rules() -> ReturnRules:
    return ReturnRules()


@lru_cache
def get_jwt_settings() -> JwtSettings:
    return JwtSettings()


@lru_cache
def get_sync_settings() -> SyncSettings:
    return SyncSettings()


@lru_cache
def get_otel_settings() -> OtelSettings:
    return OtelSettings()


@lru_cache
def get_pii_settings() -> PiiSettings:
    return PiiSettings()
