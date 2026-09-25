"""Luồng dữ liệu S4 → S5 → S6 (docs/17 §2) — giai đoạn A (ADR-010).

    bronze_ddl ─► extract_load ─► dbt.* (mỗi model một task, test ngay sau model — cosmos)

- **Chu kỳ** `PIPELINE_SCHEDULE` (cron; mặc định phút 5 mỗi giờ — cửa sổ trích một giờ vừa
  khép). Giờ khi test, ngày khi chạy thật (docs/06).
- **`catchup=False`**: lake là trạng thái, một lượt trích MỌI cửa sổ còn thiếu. Chạy bù từng lượt
  lịch cũ không thêm được gì ngoài tải.
- **Một lượt mỗi lúc**: `max_active_runs=1`, cộng advisory lock trong `run_once` cho trường hợp
  có người chạy `make pipeline-run` tay cùng lúc (lượt sau `PipelineBusyError` → retry).
- **Không có logic ở đây.** Mọi thứ nằm trong `packages/pipeline` (có test, `mypy --strict`)
  và dbt project; DAG chỉ nối chúng. Cấu hình qua biến môi trường, cùng tên với CLI.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.sdk import dag, task
from cosmos import DbtTaskGroup, ExecutionConfig, ProfileConfig, ProjectConfig, RenderConfig
from cosmos.constants import InvocationMode, LoadMode, SourceRenderingBehavior

DBT_PROJECT = Path(os.environ.get("DBT_PROJECT_DIR", "/opt/airflow/dbt"))
DBT_EXECUTABLE = os.environ.get("DBT_EXECUTABLE", "/opt/airflow/dbt-venv/bin/dbt")


@dag(
    dag_id="retail_pipeline",
    schedule=os.environ.get("PIPELINE_SCHEDULE", "5 * * * *"),
    start_date=datetime(2026, 9, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 3, "retry_delay": timedelta(minutes=2)},
    tags=["retail-sync", "data-flow"],
    doc_md=__doc__,
)
def retail_pipeline() -> None:
    @task
    def bronze_ddl() -> int:
        """Bucket + database + bảng bronze_* (CREATE ... IF NOT EXISTS — chạy lại vô hại)."""
        from pipeline.clickhouse import ClickHouse
        from pipeline.config import PipelineConfig
        from pipeline.lake import Lake

        cfg = PipelineConfig.from_env()
        Lake(cfg.lake).ensure_bucket()
        ch = ClickHouse(cfg.clickhouse)
        try:
            ch.ensure_database()
            return ch.apply_ddl()
        finally:
            ch.close()

    @task
    def extract_load() -> dict[str, dict[str, int]]:
        """S4 + S5: trích mọi cửa sổ đã an toàn, nạp mọi file chưa nạp."""
        import asyncio

        from pipeline.clickhouse import ClickHouse
        from pipeline.config import PipelineConfig
        from pipeline.lake import Lake
        from pipeline.run import run_once

        cfg = PipelineConfig.from_env()
        ch = ClickHouse(cfg.clickhouse)
        try:
            report = asyncio.run(run_once(cfg, Lake(cfg.lake), ch))
        finally:
            ch.close()
        return report.summary()

    transform = DbtTaskGroup(
        group_id="dbt",
        project_config=ProjectConfig(DBT_PROJECT, install_dbt_deps=False),
        profile_config=ProfileConfig(
            profile_name="retail_sync",
            target_name="dev",
            profiles_yml_filepath=DBT_PROJECT / "profiles.yml",
        ),
        # dbt ở venv riêng (Dockerfile) → gọi như tiến trình con. Mặc định cosmos chọn
        # DBT_RUNNER (import dbt vào tiến trình Airflow) — đúng thứ ta cố ý tách ra.
        execution_config=ExecutionConfig(
            dbt_executable_path=DBT_EXECUTABLE, invocation_mode=InvocationMode.SUBPROCESS
        ),
        render_config=RenderConfig(
            load_method=LoadMode.DBT_LS,
            dbt_executable_path=DBT_EXECUTABLE,
            invocation_mode=InvocationMode.SUBPROCESS,
            # Test trên source (bronze nhân đôi, bẫy 4) chỉ có source làm cha — không bật thì
            # cosmos bỏ mất nó.
            source_rendering_behavior=SourceRenderingBehavior.WITH_TESTS_OR_FRESHNESS,
            # Test nhiều cha (relationships fact → dim, cổng A payment = sales) chạy SAU khi mọi
            # cha xong. Mặc định cosmos gắn nó vào một cha, và nó chạy khi dim chưa dựng xong.
            should_detach_multiple_parents_tests=True,
        ),
    )

    bronze_ddl() >> extract_load() >> transform


retail_pipeline()
