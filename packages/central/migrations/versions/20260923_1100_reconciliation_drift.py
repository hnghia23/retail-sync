"""Nơi lưu lệch của job đối soát INV-4 — ràng buộc #8, AT-10, DI-1.

Revision ID: 0005_central_recon_drift
Revises: 0004_central_extract_watermarks
Create Date: 2026-09-23

`reconciliation_watermark.mismatch_count` chỉ nói "lần chạy trước lệch bao nhiêu". Nó không
nói lệch Ở ĐÂU, và lần chạy sau sẽ ghi đè con số đó. Job tăng dần chỉ xét khách CÓ biến
động mới, nên một khách lệch mà không phát sinh giao dịch nào nữa sẽ không bao giờ được
xét lại. Nếu lệch chỉ nằm trong log, nó biến mất khỏi tầm nhìn sau đúng một lần chạy.

Bảng này giữ từng lệch cho tới khi nó được xử lý (`resolved_at`). Mỗi lần chạy, job xét
thêm mọi khách còn lệch chưa xử lý — nên một bản sửa (dựng lại snapshot từ ledger) được xác
nhận tự động ở lần chạy kế tiếp. `reconcile_drift_count > 0` là cảnh báo nghiêm trọng nhất
của hệ thống (docs/08 §5): sai số điểm là sai tiền của khách.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_central_recon_drift"
down_revision: str | None = "0004_central_extract_watermarks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE reconciliation_drift (
            customer_id  uuid NOT NULL,
            field        text NOT NULL CHECK (field IN ('balance', 'lifetime_earned', 'tier', 'missing')),
            -- Giá trị suy từ ledger (nguồn sự thật) và giá trị đang nằm trong snapshot.
            expected     text NOT NULL,
            actual       text,
            detected_at  timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            resolved_at  timestamptz,
            PRIMARY KEY (customer_id, field)
        )
    """)
    op.execute(
        "CREATE INDEX reconciliation_drift_open_idx ON reconciliation_drift (customer_id)"
        " WHERE resolved_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reconciliation_drift")
