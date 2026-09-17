"""Bật pg_stat_statements — roadmap tuần 1 ngày 5, ADR-009.

Revision ID: 0002_edge_pg_stat_statements
Revises: 0001_edge_initial
Create Date: 2026-09-18

Migration RIÊNG, không gộp vào `0001_edge_initial`: sửa migration đã chạy trên DB đang
tồn tại sẽ không có tác dụng gì (Alembic không chạy lại revision đã `stamp`), phải là một
revision mới để DB đã tạo trước đó cũng nhận được thay đổi khi `upgrade head`.

`shared_preload_libraries=pg_stat_statements` đã đặt ở tham số khởi động Postgres
(`infra/compose.yaml`, không set được bằng SQL vì cần restart tiến trình). Migration này
chỉ làm phần còn lại: `CREATE EXTENSION`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_edge_pg_stat_statements"
down_revision: str | None = "0001_edge_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
