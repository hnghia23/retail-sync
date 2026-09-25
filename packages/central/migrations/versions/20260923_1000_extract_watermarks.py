"""Watermark trích xuất đáng tin — sửa bẫy 1, 2, 3 ở docs/17-data-flow.md §4.

Revision ID: 0004_central_extract_watermarks
Revises: 0003_central_ingest
Create Date: 2026-09-23

Bronze (S4) trích xuất tăng dần theo cửa sổ `[start, end)` trên `recorded_at`. Cả ba sửa đổi
dưới đây đều tìm ra khi đọc lại schema, trước khi có dòng code trích xuất nào — mỗi cái làm
dữ liệu lọt khỏi warehouse MÀ KHÔNG CÓ LỖI NÀO.

## Bẫy 1 — transaction commit muộn lọt khỏi watermark → hàm `extract_horizon()`

`recorded_at DEFAULT now()`, mà `now()` là thời điểm BẮT ĐẦU transaction, không phải lúc
commit. Một lô ingest bắt đầu lúc 10:00:59.8 và commit lúc 10:01:01.5 mang
`recorded_at = 10:00:59.8`: cửa sổ `[10:00, 10:01)` chạy lúc 10:01:00 chưa thấy nó (chưa
commit), cửa sổ `[10:01, 10:02)` thì loại nó (nhỏ hơn 10:01). Dòng đó không bao giờ vào bronze.

Cách chữa là KHÔNG đoán một độ trễ an toàn, mà hỏi thẳng Postgres: mép cuối cửa sổ =
`xact_start` nhỏ nhất của các transaction CÒN ĐANG MỞ. Mọi dòng có `recorded_at` nhỏ hơn mốc
đó đều thuộc transaction đã kết thúc (commit hoặc rollback), nên đọc sau mốc đó là đủ và
không bao giờ còn dòng nào "đến sau" với `recorded_at` nhỏ hơn. Một transaction treo lâu làm
trích xuất ĐỨNG lại (thấy được qua chỉ số độ trễ), chứ không làm MẤT dòng. Đứng thì an toàn,
lọt thì không.

Hàm từ chối chạy khi role gọi không đọc được `xact_start` của phiên khác
(`<insufficient privilege>`): khi đó `min()` bỏ qua các dòng NULL và trả về một mốc SAI mà
không báo gì — đúng loại lỗi im lặng mà hàm này sinh ra để chặn. Role trích xuất ở production
cần `GRANT pg_read_all_stats`.

## Bẫy 2 — `point_ledger` không có index trên `recorded_at`

Bảng phân vùng theo `occurred_at`, trích xuất lọc theo `recorded_at`: không index thì mỗi
lần trích xuất quét toàn bộ mọi partition. Index tạo trên bảng cha được Postgres tự nhân ra
mọi partition, kể cả partition `ensure_point_ledger_partitions()` tạo về sau.

## Bẫy 3 — bảng bị UPDATE mà không chạm cột watermark

`CustomerUpdated` sửa `customer` tại chỗ, mà bảng không có cột nào ghi lúc dòng đổi → trích
xuất tăng dần không bao giờ thấy bản cập nhật. Quy tắc chung (docs/17 §4): *bảng nào được
trích xuất tăng dần thì MỌI lần ghi đều phải chạm cột watermark.* Cưỡng chế bằng trigger
chứ không bằng kỷ luật ở từng handler: handler mới (VD `SaleVoided` sau này sửa
`sale_replica.status`) quên set cột thì trigger vẫn set. Trigger cũng ghi đè giá trị client
tự đặt — `recorded_at` là giờ TRUNG TÂM nhận, không ai được khai hộ.

Giá trị là `now()` (đầu transaction) chứ không `clock_timestamp()`: đó đúng là mốc mà
`extract_horizon()` lập luận. Đổi sang `clock_timestamp()` là phá bẫy 1.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_central_extract_watermarks"
down_revision: str | None = "0003_central_ingest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bảng trích xuất tăng dần theo `recorded_at`. `point_ledger` không có trong danh sách: bảng
# append-only (ADR-002), không có UPDATE nào để lọt, và `DEFAULT now()` đã đúng mốc.
_RECORDED_AT_TABLES = ("customer", "sale_replica", "shift_replica")


def upgrade() -> None:
    # ── Bẫy 2 ──
    op.execute("CREATE INDEX point_ledger_recorded_idx ON point_ledger (recorded_at)")

    # ── Bẫy 3 ──
    # Dòng đã có nhận mốc = lúc migrate: lần trích xuất đầu sẽ lấy trọn bảng, đúng ý.
    op.execute("ALTER TABLE customer ADD COLUMN recorded_at timestamptz NOT NULL DEFAULT now()")
    op.execute("CREATE INDEX customer_recorded_idx ON customer (recorded_at)")
    op.execute("CREATE INDEX shift_replica_recorded_idx ON shift_replica (recorded_at)")
    # `dim_customer` cần hạng hiện tại → `point_balance` cũng được trích xuất, watermark là
    # `updated_at` (upsert trong handler đã set, trigger biến điều đó thành bắt buộc).
    op.execute("CREATE INDEX point_balance_updated_idx ON point_balance (updated_at)")

    op.execute("""
        CREATE FUNCTION touch_recorded_at() RETURNS trigger LANGUAGE plpgsql AS $fn$
        BEGIN
            NEW.recorded_at := now();
            RETURN NEW;
        END
        $fn$
    """)
    op.execute("""
        CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $fn$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END
        $fn$
    """)
    for table in _RECORDED_AT_TABLES:
        op.execute(f"""
            CREATE TRIGGER {table}_touch_recorded_at
            BEFORE INSERT OR UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION touch_recorded_at()
        """)
    op.execute("""
        CREATE TRIGGER point_balance_touch_updated_at
        BEFORE INSERT OR UPDATE ON point_balance
        FOR EACH ROW EXECUTE FUNCTION touch_updated_at()
    """)

    # ── Bẫy 1 ──
    # `coalesce(xact_start, query_start)`: có một khe rất nhỏ giữa lúc backend nhận câu lệnh
    # (mốc `now()` của transaction lấy từ đây) và lúc nó báo `xact_start` lên
    # pg_stat_activity. Trong khe đó `query_start` đã có — dùng nó thay cho NULL. Cộng thêm
    # `safety_lag` mặc định 30 giây là thừa đủ cho khe cỡ micro-giây này.
    op.execute("""
        CREATE FUNCTION extract_horizon(safety_lag interval DEFAULT interval '30 seconds')
        RETURNS timestamptz LANGUAGE plpgsql VOLATILE AS $fn$
        DECLARE
            hidden integer;
            oldest timestamptz;
        BEGIN
            -- Đếm dòng bị ẩn TRƯỚC khi lọc `backend_type`: với phiên của role khác, Postgres
            -- ẩn cả `backend_type` (NULL), nên lọc nó ở WHERE sẽ loại đúng những dòng cần
            -- đếm — lời chặn thành vô dụng. Test đã bắt được đúng lỗi này ở bản đầu.
            SELECT count(*) FILTER (WHERE query = '<insufficient privilege>'),
                   min(coalesce(xact_start, query_start))
                       FILTER (WHERE backend_type = 'client backend'
                                 AND (state <> 'idle' OR xact_start IS NOT NULL))
              INTO hidden, oldest
              FROM pg_stat_activity
             WHERE datname = current_database()
               AND pid <> pg_backend_pid();

            IF hidden > 0 THEN
                RAISE EXCEPTION
                    'extract_horizon: khong doc duoc xact_start cua % phien khac '
                    '(thieu quyen pg_read_all_stats) - moc tinh ra se SAI ma khong bao loi',
                    hidden
                    USING ERRCODE = 'insufficient_privilege';
            END IF;

            RETURN least(clock_timestamp() - safety_lag, coalesce(oldest, 'infinity'));
        END
        $fn$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS extract_horizon(interval)")
    op.execute("DROP TRIGGER IF EXISTS point_balance_touch_updated_at ON point_balance")
    for table in _RECORDED_AT_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_touch_recorded_at ON {table}")
    op.execute("DROP FUNCTION IF EXISTS touch_updated_at()")
    op.execute("DROP FUNCTION IF EXISTS touch_recorded_at()")
    op.execute("DROP INDEX IF EXISTS point_balance_updated_idx")
    op.execute("DROP INDEX IF EXISTS shift_replica_recorded_idx")
    op.execute("DROP INDEX IF EXISTS customer_recorded_idx")
    op.execute("ALTER TABLE customer DROP COLUMN IF EXISTS recorded_at")
    op.execute("DROP INDEX IF EXISTS point_ledger_recorded_idx")
