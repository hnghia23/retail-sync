"""Cấu hình pipeline — đọc từ biến môi trường, không phụ thuộc pydantic-settings.

Image Airflow có bộ dependency riêng (data_platform/requirements.txt); giữ package này chỉ
cần thư viện chuẩn + asyncpg + pyarrow + httpx để chạy được ở cả hai nơi.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["ClickHouseConfig", "LakeConfig", "PipelineConfig"]


@dataclass(frozen=True, slots=True)
class LakeConfig:
    #: `host:port` mà TIẾN TRÌNH NÀY dùng để ghi (ví dụ `localhost:9000` từ máy dev).
    endpoint: str
    access_key: str
    secret_key: str
    #: URL mà CLICKHOUSE dùng để đọc cùng bucket — trong compose là `http://minio:9000`, khác
    #: với `endpoint` vì hai tiến trình nhìn mạng khác nhau.
    url_for_clickhouse: str
    bucket: str = "lake"
    prefix: str = "bronze/central"
    scheme: str = "http"


@dataclass(frozen=True, slots=True)
class ClickHouseConfig:
    url: str
    user: str
    password: str
    database: str = "dw"


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    #: DSN Postgres TRUNG TÂM (asyncpg, dạng `postgresql://`). Nguồn trích xuất duy nhất.
    pg_dsn: str
    lake: LakeConfig
    clickhouse: ClickHouseConfig
    #: Độ dài một cửa sổ trích xuất. Đổi giữa chừng an toàn: cửa sổ mới bắt đầu từ mép cuối
    #: của file cuối cùng trong lake, nên không bao giờ chồng lên nhau.
    window_seconds: int = 3600
    #: Cộng thêm vào `extract_horizon()` — chỉ che khe micro-giây (docs/17 §4 bẫy 1).
    safety_lag_seconds: float = 30.0
    #: Bộ giám sát: cửa hàng mà cảnh báo "im lặng quá 1 giờ" canh. Rỗng = mọi cửa hàng
    #: (production). `FLOW_MONITOR_WATCH_STORES=store-001,store-002`.
    watch_stores: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> PipelineConfig:
        env = os.environ

        def need(name: str) -> str:
            value = env.get(name, "")
            if not value:
                raise SystemExit(f"Thiếu biến môi trường {name}")
            return value

        return cls(
            pg_dsn=need("PIPELINE_PG_DSN"),
            lake=LakeConfig(
                endpoint=env.get("LAKE_ENDPOINT", "localhost:9000"),
                access_key=need("LAKE_ACCESS_KEY"),
                secret_key=need("LAKE_SECRET_KEY"),
                url_for_clickhouse=env.get("LAKE_URL_FOR_CLICKHOUSE", "http://minio:9000"),
                bucket=env.get("LAKE_BUCKET", "lake"),
                prefix=env.get("LAKE_PREFIX", "bronze/central"),
            ),
            clickhouse=ClickHouseConfig(
                url=env.get("CLICKHOUSE_URL", "http://localhost:8123"),
                user=env.get("CLICKHOUSE_USER", "dw"),
                password=need("CLICKHOUSE_PASSWORD"),
                database=env.get("CLICKHOUSE_DB", "dw"),
            ),
            window_seconds=int(env.get("PIPELINE_WINDOW_SECONDS", "3600")),
            safety_lag_seconds=float(env.get("PIPELINE_SAFETY_LAG_SECONDS", "30")),
            watch_stores=tuple(
                s.strip() for s in env.get("FLOW_MONITOR_WATCH_STORES", "").split(",") if s.strip()
            ),
        )
