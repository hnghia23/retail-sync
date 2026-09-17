"""Cấu hình Edge API. Biến môi trường do infra/compose.yaml cấp."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.config import JwtSettings, OtelSettings, PricingRules, ReturnRules, SyncSettings


class EdgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "infra/.env"), env_file_encoding="utf-8", extra="ignore"
    )

    store_id: str = "store-001"
    database_url: str = "postgresql+asyncpg://edge_app:changeme-edge@localhost:5433/edge_store_001"
    redis_url: str = "redis://localhost:6380/0"
    central_api_url: str = "http://localhost:8000"

    # docs/13 §4 — ngân sách tra cứu khách ở client. Vượt thì coi như khách ẩn danh,
    # KHÔNG chặn bán hàng (nguyên tắc kiến trúc #1).
    central_lookup_timeout_seconds: float = 0.5
    customer_cache_ttl_seconds: int = 900  # 15 phút

    # Cảnh báo master data cũ — B02 E03
    master_data_stale_after_hours: int = 24

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
