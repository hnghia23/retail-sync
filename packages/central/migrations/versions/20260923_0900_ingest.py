"""Chuẩn bị cho ingest — roadmap tuần 2 ngày 8 (`POST /events`, docs/13 §2).

Revision ID: 0003_central_ingest
Revises: 0002_central_pg_stat_statements
Create Date: 2026-09-23

Hai thay đổi, cả hai đều lộ ra khi viết handler ingest chứ không lộ ra lúc thiết kế schema:

## 1. `customer.phone_hash` thôi UNIQUE — case C03 (docs/business/02-edge-cases.md)

Hai cửa hàng cùng offline có thể cùng đăng ký một SĐT (C02 cho phép tạo khách khi offline).
Cả hai sinh `customer_id` riêng và tích điểm vào ID của mình. Khi đồng bộ về, `CustomerCreated`
thứ hai đụng UNIQUE và rơi vào một trong hai ngõ cụt:

  - coi là lỗi thử lại được → kẹt mãi, rồi dead-letter sau N lần;
  - coi là lỗi vĩnh viễn → khách thứ hai không bao giờ tồn tại ở trung tâm.

Ngõ nào cũng dẫn tới cùng kết cục: mọi `PointsEarned` của khách thứ hai vỡ khóa ngoại
`point_ledger.customer_id → customer`, tức **điểm thật của khách bị mất khỏi trung tâm**.
UNIQUE ở đây làm chính tình huống C03 không thể GHI NHẬN, trong khi thiết kế đòi "phát
hiện tự động, gộp thủ công". Giờ trùng SĐT được ghi cả hai bản ghi và đưa vào
`customer_duplicate_candidate` để người gộp.

Phía cửa hàng GIỮ unique (`customer_local_phone_hash_idx`): trong phạm vi một cửa hàng thì
một SĐT đúng là một khách.

## 2. `store_credential` — khóa API theo cửa hàng (docs/13 §2)

`POST /events` nhận `store_id` trong envelope. Không xác thực thì cửa hàng A ghi được sự
kiện mang danh cửa hàng B — cùng lớp lỗi với việc `POST /sales` tin `employee_id` client
gửi lên (đã vá ở tuần 1). Trung tâm đối chiếu `store_id` trong từng envelope với cửa hàng
sở hữu khóa.

Lưu SHA-256 của khóa, KHÔNG dùng Argon2: khóa là 32 byte ngẫu nhiên, không phải mật khẩu
người đặt, nên không có không gian đoán để làm chậm — và Argon2 cố tình tốn ~50ms mỗi lần
kiểm, ăn gần hết ngân sách 300ms của route tra cứu (docs/13 §2). Đây là cách chuẩn cho
token API sinh bằng máy.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_central_ingest"
down_revision: str | None = "0002_central_pg_stat_statements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE customer DROP CONSTRAINT customer_phone_hash_key")
    # Vẫn cần index: tra khách theo SĐT (`/customers/by-phone`) và dò trùng đều đi qua cột này.
    op.execute("CREATE INDEX customer_phone_hash_idx ON customer (phone_hash)")

    op.execute("""
        CREATE TABLE store_credential (
            store_id   text PRIMARY KEY REFERENCES store (store_id),
            -- hex SHA-256 của khóa. Khóa gốc chỉ in ra MỘT lần lúc cấp
            -- (`central.ops.provision_store`) — mất thì cấp khóa mới, không khôi phục.
            key_sha256 text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            -- Thu hồi: đặt cột này thay vì xóa dòng, để còn dấu vết khóa từng tồn tại.
            revoked_at timestamptz
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS store_credential")
    op.execute("DROP INDEX IF EXISTS customer_phone_hash_idx")
    op.execute("ALTER TABLE customer ADD CONSTRAINT customer_phone_hash_key UNIQUE (phone_hash)")
