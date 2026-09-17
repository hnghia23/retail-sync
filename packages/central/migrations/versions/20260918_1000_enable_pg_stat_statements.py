"""Bật pg_stat_statements — roadmap tuần 1 ngày 5, ADR-009.

Revision ID: 0002_central_pg_stat_statements
Revises: 0001_central_initial
Create Date: 2026-09-18

Xem docstring đầy đủ ở bản edge (`packages/edge/migrations/versions/..._enable_pg_stat_statements.py`)
— cùng lý do, cùng cơ chế, chỉ khác DB đích.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_central_pg_stat_statements"
down_revision: str | None = "0001_central_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
