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
