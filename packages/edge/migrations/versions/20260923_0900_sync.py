"""Chuẩn bị cho sync worker — roadmap tuần 2 ngày 8 (ADR-003, docs/14 §4).

Revision ID: 0003_edge_sync
Revises: 0002_edge_pg_stat_statements
Create Date: 2026-09-23

## 1. `outbox.next_attempt_at` — backoff THEO TỪNG SỰ KIỆN

Không có cột này, một sự kiện bị trung tâm từ chối (tạm thời) sẽ được lấy lại ở vòng quét
kế tiếp — tức 2 giây sau — và đốt hết `max_attempts` lượt trong khoảng 20 giây rồi rơi vào
dead-letter. Trường hợp điển hình: `PointsEarned` vỡ khóa ngoại vì `CustomerCreated` của
khách đó đang lỗi tạm; 20 giây thường không đủ để nó qua.

Backoff của LÔ (khi cả trung tâm không tới được) KHÔNG dùng cột này và KHÔNG tăng `attempts`
— xem `edge.sync.worker`. Mất mạng là lỗi của đường truyền, không phải của sự kiện; nếu tính
vào lượt thử thì cửa hàng offline vài phút đã tự vứt dữ liệu của mình vào dead-letter.

## 2. Trigger `outbox.sent_at` → `point_ledger_local.synced_at`

`LoyaltyService.snapshot_for()` hiển thị số dư = số dư trung tâm + tổng dòng ledger cục bộ
có `synced_at IS NULL` (docs/03 §4.3). Trước migration này KHÔNG CÓ GÌ set cột đó, nên sau khi
đồng bộ, điểm sẽ bị cộng hai lần trên màn hình quầy.

Vì sao trigger mà không để sync worker tự UPDATE: worker bị `import-linter` cấm biết về
loyalty (hợp đồng `sync-is-transport-only`), và ADR-004 cấm module này truy vấn bảng của
module kia. Mối nối giữa hai bảng là bất biến "`event_id` của dòng ledger CHÍNH LÀ `event_id`
của sự kiện outbox" (docs/12 §3.3) — một sự thật của schema, nên nó sống trong schema. Cùng
transaction với lệnh đánh dấu `sent_at`, nên không có khoảnh khắc nào hai bên lệch nhau.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_edge_sync"
down_revision: str | None = "0002_edge_pg_stat_statements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE outbox ADD COLUMN next_attempt_at timestamptz")

    op.execute("""
        CREATE FUNCTION mark_point_ledger_synced() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            UPDATE point_ledger_local
            SET synced_at = NEW.sent_at
            WHERE event_id = NEW.event_id AND synced_at IS NULL;
            RETURN NULL;
        END $fn$
    """)
    # `WHEN` lọc ngay ở tầng trigger: chỉ lượt chuyển NULL → có giá trị mới đáng xử lý. Các
    # UPDATE khác trên outbox (tăng `attempts`, đặt `next_attempt_at`) không tốn một lần gọi hàm.
    op.execute("""
        CREATE TRIGGER outbox_sent_marks_ledger_synced
        AFTER UPDATE OF sent_at ON outbox
        FOR EACH ROW
        WHEN (OLD.sent_at IS NULL AND NEW.sent_at IS NOT NULL)
        EXECUTE FUNCTION mark_point_ledger_synced()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS outbox_sent_marks_ledger_synced ON outbox")
    op.execute("DROP FUNCTION IF EXISTS mark_point_ledger_synced()")
    op.execute("ALTER TABLE outbox DROP COLUMN IF EXISTS next_attempt_at")
