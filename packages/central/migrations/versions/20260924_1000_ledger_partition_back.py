"""Partition `point_ledger` cho cả THÁNG TRƯỚC — cửa hàng offline vắt qua cuối tháng.

Revision ID: 0006_central_ledger_back_part
Revises: 0005_central_recon_drift
Create Date: 2026-09-24

Phát hiện bằng bộ giả lập chế độ `virtual` (tật `offline` qua đêm 31/8 → 1/9, docs/18 §5):
`ensure_point_ledger_partitions()` chỉ tạo tháng hiện tại + N tháng tới. Cửa hàng mất mạng từ
tối 31/8, đồng bộ lại sáng 1/9 → điểm của ngày 31/8 cần partition THÁNG 8. Khi hệ thống đã chạy
vài tháng thì partition đó có sẵn (được tạo trước từ tháng 5). Nhưng ở tháng go-live, và ở MỌI
môi trường mới (staging, test, khôi phục DR), nó không tồn tại:

  - trung tâm trả `integrity_violation: 23514` (no partition for row) — lỗi KHÔNG thử lại được;
  - sự kiện điểm vào dead-letter, đơn thì vẫn vào → khách mất điểm, âm thầm.

Sửa: hàm nhận thêm `months_back` (mặc định 1). Không lùi xa hơn: sự kiện cũ hơn một tháng mà
không có partition là đồng hồ cửa hàng sai hoặc dữ liệu quá cũ, và từ chối ồn ào (không có
DEFAULT partition, xem `test_insert_beyond_partitions_fails_loudly`) vẫn là đúng.

Phải DROP rồi CREATE: `CREATE OR REPLACE` với danh sách tham số khác tạo ra hàm quá tải THỨ HAI,
và mọi lời gọi `ensure_point_ledger_partitions(3)` hiện có trở thành mơ hồ.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_central_ledger_back_part"
down_revision: str | None = "0005_central_recon_drift"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BODY = """
        LANGUAGE plpgsql AS $fn$
        DECLARE
            v_month   date := date_trunc('month', now())::date;
            v_i       integer;
            v_from    date;
            v_to      date;
            v_name    text;
            v_created integer := 0;
        BEGIN
            IF months_ahead < 1 THEN
                RAISE EXCEPTION 'months_ahead phai >= 1 (nhan duoc %)', months_ahead;
            END IF;
            {back_check}
            FOR v_i IN {first}..months_ahead LOOP
                v_from := v_month + (v_i || ' month')::interval;
                v_to   := v_from + interval '1 month';
                v_name := 'point_ledger_' || to_char(v_from, 'YYYY_MM');

                IF to_regclass(v_name) IS NULL THEN
                    EXECUTE format(
                        'CREATE TABLE %I PARTITION OF point_ledger FOR VALUES FROM (%L) TO (%L)',
                        v_name, v_from, v_to
                    );
                    v_created := v_created + 1;
                END IF;
            END LOOP;

            RETURN v_created;
        END $fn$
"""


def upgrade() -> None:
    op.execute("DROP FUNCTION ensure_point_ledger_partitions(integer)")
    op.execute(
        "CREATE FUNCTION ensure_point_ledger_partitions("
        "months_ahead integer DEFAULT 3, months_back integer DEFAULT 1) RETURNS integer"
        + _BODY.format(
            back_check="IF months_back < 0 THEN RAISE EXCEPTION"
            " 'months_back phai >= 0 (nhan duoc %)', months_back; END IF;",
            first="-months_back",
        )
    )
    op.execute("SELECT ensure_point_ledger_partitions(3)")


def downgrade() -> None:
    # Partition tháng trước đã tạo thì giữ nguyên: xóa nó là xóa dữ liệu điểm.
    op.execute("DROP FUNCTION ensure_point_ledger_partitions(integer, integer)")
    op.execute(
        "CREATE FUNCTION ensure_point_ledger_partitions(months_ahead integer DEFAULT 3)"
        " RETURNS integer" + _BODY.format(back_check="", first="0")
    )
