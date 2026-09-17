"""Schema trung tâm — docs/05-data-model.md §4

Revision ID: 0001_central_initial
Revises:
Create Date: 2026-09-17

Trung tâm sở hữu master data, cửa hàng sở hữu giao dịch, điểm thì hợp nhất (docs/05 §2).
Bảng `*_replica` là bản sao giao dịch nhận qua outbox — trung tâm KHÔNG ghi vào chúng từ
bất cứ nguồn nào khác.

Hai điểm khác biệt so với DDL minh họa ở docs/05 §4.3, cần biết trước khi đọc:

1. `point_ledger` PARTITION BY RANGE (occurred_at) nên PRIMARY KEY buộc phải chứa cột phân
   vùng: PK là `(event_id, occurred_at)`, không phải `event_id` một mình — Postgres không
   cho phép khác. Điều này KHÔNG làm yếu idempotency: chốt chặn chống lặp là
   `processed_event(event_id)` (bảng không phân vùng, docs/12 §5), còn `point_ledger` chỉ
   được ghi sau khi `claim_event()` trả về true.

2. KHÔNG có `CHECK (balance >= 0)` trên `point_balance`, vì `RETURN_ALLOW_NEGATIVE_BALANCE`
   mặc định là `true` (docs/05 §5). Đây là hệ quả cấu trúc: đổi cấu hình đó sang `false`
   là một migration thêm constraint, không phải sửa biến môi trường.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_central_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _master_data()
    _customer()
    _point_ledger()
    _replicas()
    _operational_tables()


# ══════════════════════ 4.1 — Master data ══════════════════════


def _master_data() -> None:
    """`version` trên mỗi bảng là cơ sở của việc kéo delta (FR-C01).

    Quy tắc thời gian hiệu lực: `promotion`, `tier_rule`, `earn_rule` đều có
    `valid_from`/`valid_to`. Giao dịch áp quy tắc CÓ HIỆU LỰC TẠI THỜI ĐIỂM MỞ ĐƠN, không
    phải quy tắc hiện tại (B02 E12, L05). Vì vậy không bao giờ UPDATE đè lên một quy tắc
    đang chạy — đóng nó lại bằng `valid_to` rồi chèn dòng mới.
    """
    op.execute("""
        CREATE TABLE region (
            region_id        text PRIMARY KEY,
            name             text NOT NULL,
            parent_region_id text REFERENCES region (region_id)
        )
    """)
    op.execute("""
        CREATE TABLE store (
            store_id            text PRIMARY KEY,
            name                text NOT NULL,
            address             text,
            city                text,
            region_id           text REFERENCES region (region_id),
            manager_employee_id text,
            opened_at           timestamptz,
            closed_at           timestamptz,
            status              text NOT NULL DEFAULT 'ACTIVE'
                                CHECK (status IN ('ACTIVE', 'CLOSED', 'SUSPENDED')),
            version             bigint NOT NULL DEFAULT 1
        )
    """)
    op.execute("""
        CREATE TABLE category (
            category_id        text PRIMARY KEY,
            name               text NOT NULL,
            parent_category_id text REFERENCES category (category_id)
        )
    """)
    op.execute("""
        CREATE TABLE product (
            product_id   text PRIMARY KEY,
            sku          text NOT NULL,
            barcode      text,
            name         text NOT NULL,
            category_id  text REFERENCES category (category_id),
            unit_price   bigint NOT NULL CHECK (unit_price >= 0),
            is_sellable  boolean NOT NULL DEFAULT true,
            is_orderable boolean NOT NULL DEFAULT true,
            version      bigint NOT NULL DEFAULT 1
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX product_barcode_idx ON product (barcode) WHERE barcode IS NOT NULL"
    )
    op.execute("""
        -- Nuôi SCD2 của dim_product trong warehouse (docs/05 §7).
        CREATE TABLE product_price_history (
            product_id text NOT NULL REFERENCES product (product_id),
            price      bigint NOT NULL CHECK (price >= 0),
            valid_from timestamptz NOT NULL,
            valid_to   timestamptz,
            PRIMARY KEY (product_id, valid_from)
        )
    """)
    op.execute("""
        CREATE TABLE warehouse (
            warehouse_id text PRIMARY KEY,
            name         text NOT NULL,
            region_id    text REFERENCES region (region_id),
            capacity     integer
        )
    """)
    op.execute("""
        CREATE TABLE promotion (
            promotion_id text PRIMARY KEY,
            name         text NOT NULL,
            rule         jsonb NOT NULL,
            valid_from   timestamptz NOT NULL,
            valid_to     timestamptz,
            version      bigint NOT NULL DEFAULT 1
        )
    """)
    op.execute("""
        CREATE TABLE tier_rule (
            tier                text PRIMARY KEY,
            min_lifetime_points integer NOT NULL CHECK (min_lifetime_points >= 0),
            discount_pct        integer NOT NULL CHECK (discount_pct BETWEEN 0 AND 100),
            version             bigint NOT NULL DEFAULT 1
        )
    """)
    op.execute("""
        CREATE TABLE earn_rule (
            rule_id       text PRIMARY KEY,
            vnd_per_point bigint NOT NULL CHECK (vnd_per_point > 0),
            multiplier    numeric(6,3) NOT NULL DEFAULT 1.0 CHECK (multiplier >= 0),
            valid_from    timestamptz NOT NULL,
            valid_to      timestamptz,
            version       bigint NOT NULL DEFAULT 1
        )
    """)
    op.execute("""
        -- ⚠️ KHÔNG có cột `salary`: dữ liệu HR, không thuộc hệ thống này (docs/05 §4.1).
        CREATE TABLE employee (
            employee_id   text PRIMARY KEY,
            store_id      text REFERENCES store (store_id),
            name          text NOT NULL,
            role          text NOT NULL CHECK (role IN ('cashier', 'manager', 'region_manager', 'admin')),
            password_hash text NOT NULL,
            hired_at      date,
            left_at       date,
            revoked_at    timestamptz,
            status        text NOT NULL DEFAULT 'ACTIVE'
                          CHECK (status IN ('ACTIVE', 'INACTIVE', 'REVOKED')),
            version       bigint NOT NULL DEFAULT 1
        )
    """)


# ══════════════════════ 4.2 — Khách hàng ══════════════════════


def _customer() -> None:
    op.execute("""
        CREATE TABLE customer (
            customer_id uuid PRIMARY KEY DEFAULT uuidv7(),
            -- Ràng buộc #10: SĐT không bao giờ lưu dạng đọc được. `phone_hash` để TRA,
            -- `phone_enc` để HIỂN THỊ sau khi giải mã có kiểm soát.
            phone_hash  text NOT NULL UNIQUE,
            phone_enc   bytea,
            name_enc    bytea,
            joined_at   timestamptz NOT NULL DEFAULT now(),
            status      text NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE', 'MERGED', 'BLOCKED')),
            -- C03: gộp khách trùng. Bản ghi cũ KHÔNG bị xóa — điểm của nó đã nằm trong
            -- ledger và ledger là append-only.
            merged_into uuid REFERENCES customer (customer_id),

            CONSTRAINT customer_merged_has_target
                CHECK (status <> 'MERGED' OR merged_into IS NOT NULL)
        )
    """)


# ══════════════════════ 4.3 — Sổ cái điểm (nguồn sự thật) ══════════════════════


def _point_ledger() -> None:
    """ADR-002. Xem ghi chú (1) và (2) ở docstring module về PK và CHECK số dư."""
    op.execute("""
        CREATE TABLE point_ledger (
            -- `event_id` sinh TẠI CỬA HÀNG và giữ nguyên suốt đường đi (docs/12 §3.3).
            event_id    uuid NOT NULL,
            customer_id uuid NOT NULL REFERENCES customer (customer_id),
            store_id    text NOT NULL,
            sale_id     uuid,
            delta       integer NOT NULL CHECK (delta <> 0),
            reason      text NOT NULL
                        CHECK (reason IN ('EARN', 'REDEEM', 'RETURN', 'EXPIRE', 'ADJUST')),
            -- Thời điểm TẠI CỬA HÀNG. Là khóa phân vùng.
            occurred_at timestamptz NOT NULL,
            -- Thời điểm TẠI TRUNG TÂM. Chênh lệch giữa hai cột này là thước đo độ trễ đồng
            -- bộ và là cách phát hiện lệch đồng hồ ở cửa hàng.
            recorded_at timestamptz NOT NULL DEFAULT now(),
            metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,

            PRIMARY KEY (event_id, occurred_at)
        ) PARTITION BY RANGE (occurred_at)
    """)
    op.execute("""
        CREATE INDEX point_ledger_customer_idx ON point_ledger (customer_id, occurred_at DESC)
    """)
    op.execute("CREATE INDEX point_ledger_store_idx ON point_ledger (store_id, occurred_at)")

    op.execute("""
        CREATE TABLE point_balance (
            customer_id     uuid PRIMARY KEY REFERENCES customer (customer_id),
            -- Snapshot. Dựng lại được hoàn toàn từ ledger — đó là điều job đối soát
            -- hằng ngày kiểm (INV-4, DI-1, AT-10).
            balance         integer NOT NULL DEFAULT 0,
            -- ⚠️ Ràng buộc #6: XẾP HẠNG DÙNG CỘT NÀY, không phải `balance`.
            -- Tiêu điểm không được làm tụt hạng khách.
            lifetime_earned integer NOT NULL DEFAULT 0 CHECK (lifetime_earned >= 0),
            tier            text NOT NULL DEFAULT 'BRONZE',
            last_event_at   timestamptz,
            updated_at      timestamptz NOT NULL DEFAULT now()
            -- KHÔNG có CHECK (balance >= 0) — xem ghi chú (2) ở docstring module.
        )
    """)

    _partition_management()


def _partition_management() -> None:
    """Ràng buộc #2 — rủi ro sập CAO NHẤT của cả thiết kế (docs/08 §4.1).

    Postgres không tự tạo partition tương lai. Hết partition nghĩa là MỌI `INSERT` vào
    `point_ledger` lỗi, tức toàn bộ hệ thống điểm chết — và nó chết đúng vào 00:00 ngày
    đầu tháng, lúc không ai trực.

    Không dùng `pg_partman` vì image `postgres:18` chuẩn không có extension đó; cài thêm
    nghĩa là image tùy biến cho mọi môi trường. Một hàm SQL ~20 dòng làm đúng việc cần.

    KHÔNG tạo DEFAULT partition: nó biến lỗi ồn ào (insert fail) thành lỗi im lặng (dữ
    liệu rơi vào default), và sau đó việc `ATTACH` partition đúng tháng sẽ thất bại vì
    default đã chứa dòng thuộc khoảng đó. Thà hỏng to và sớm.
    """
    op.execute("""
        CREATE FUNCTION ensure_point_ledger_partitions(months_ahead integer DEFAULT 3)
        RETURNS integer
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

            -- Tạo cả tháng hiện tại (i = 0) lẫn months_ahead tháng tới.
            FOR v_i IN 0..months_ahead LOOP
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
    """)
    op.execute("""
        -- View cho kiểm tra HẰNG NGÀY (docs/08 §4.1). Cảnh báo khi `months_ahead` tụt
        -- xuống dưới ngưỡng cấu hình — đừng đợi tới lúc INSERT lỗi mới biết.
        CREATE VIEW point_ledger_partition_health AS
        SELECT
            count(*)::integer AS partition_count,
            max(
                (regexp_replace(c.relname, '^point_ledger_', '') || '_01')::date
            ) AS last_partition_month,
            (
                EXTRACT(YEAR  FROM age(
                    max((regexp_replace(c.relname, '^point_ledger_', '') || '_01')::date),
                    date_trunc('month', now())::date)) * 12
              + EXTRACT(MONTH FROM age(
                    max((regexp_replace(c.relname, '^point_ledger_', '') || '_01')::date),
                    date_trunc('month', now())::date))
            )::integer AS months_ahead
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = 'point_ledger'
    """)
    # Tạo ngay partition cho tháng hiện tại + 3 tháng tới, để hệ thống chạy được từ giây đầu.
    op.execute("SELECT ensure_point_ledger_partitions(3)")


# ══════════════════════ 4.4 — Bản sao giao dịch ══════════════════════


def _replicas() -> None:
    """Bản sao giao dịch từ cửa hàng — nguồn sự thật vẫn là cửa hàng (docs/05 §2).

    Cố ý KHÔNG có CHECK/trigger bất biến ở đây: bất biến đã được cưỡng chế ở nơi dữ liệu
    ra đời (DB cửa hàng) và ở tầng sự kiện (`shared.events`). Lặp lại lần thứ ba tại đây
    chỉ tạo khả năng một sự kiện hợp lệ bị từ chối ở đầu nhận và kẹt vĩnh viễn trong
    outbox — biến lỗi dữ liệu thành lỗi khả dụng. Sai lệch được phát hiện bằng job đối
    soát (DI-2, DI-3), không bằng cách chặn ingest.
    """
    op.execute("""
        CREATE TABLE shift_replica (
            shift_id      uuid PRIMARY KEY,
            store_id      text NOT NULL REFERENCES store (store_id),
            business_date date NOT NULL,
            opened_by_employee_id text NOT NULL,
            closed_by_employee_id text,
            opened_at     timestamptz NOT NULL,
            closed_at     timestamptz,
            opening_cash  bigint NOT NULL DEFAULT 0,
            expected_cash bigint,
            counted_cash  bigint,
            variance      bigint,
            variance_note text,
            recorded_at   timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX shift_replica_store_date_idx ON shift_replica (store_id, business_date)"
    )

    op.execute("""
        CREATE TABLE sale_replica (
            sale_id       uuid PRIMARY KEY,
            store_id      text NOT NULL REFERENCES store (store_id),
            shift_id      uuid,
            business_date date NOT NULL,
            employee_id   text NOT NULL,
            customer_id   uuid,
            occurred_at   timestamptz NOT NULL,
            -- Ngày NẠP tại trung tâm. Bronze phân vùng theo cột này, không theo
            -- `occurred_at` (ràng buộc #3) — để dữ liệu đến trễ không phải viết lại lịch sử.
            recorded_at   timestamptz NOT NULL DEFAULT now(),

            subtotal        bigint NOT NULL,
            discount_tier   bigint NOT NULL DEFAULT 0,
            discount_promo  bigint NOT NULL DEFAULT 0,
            total           bigint NOT NULL,
            tendered_amount bigint,
            change_amount   bigint,
            promotion_id    text,
            authorized_by_employee_id text,
            status          text NOT NULL DEFAULT 'COMPLETED',
            voided_by_sale_id uuid,
            original_sale_id  uuid
        )
    """)
    op.execute("CREATE INDEX sale_replica_store_date_idx ON sale_replica (store_id, business_date)")
    op.execute(
        "CREATE INDEX sale_replica_customer_idx ON sale_replica (customer_id, occurred_at DESC)"
    )
    # Trích xuất tăng dần cho bronze quét theo watermark trên cột này (docs/03 §6).
    op.execute("CREATE INDEX sale_replica_recorded_idx ON sale_replica (recorded_at)")

    op.execute("""
        CREATE TABLE sale_line_replica (
            sale_id    uuid NOT NULL REFERENCES sale_replica (sale_id) ON DELETE CASCADE,
            line_no    integer NOT NULL,
            product_id text NOT NULL,
            quantity   integer NOT NULL,
            unit_price bigint NOT NULL,
            line_total bigint NOT NULL,
            original_sale_id      uuid,
            original_sale_line_no integer,
            PRIMARY KEY (sale_id, line_no)
        )
    """)
    op.execute("""
        CREATE TABLE sale_payment_replica (
            sale_id   uuid NOT NULL REFERENCES sale_replica (sale_id) ON DELETE CASCADE,
            seq       integer NOT NULL,
            method    text NOT NULL,
            amount    bigint NOT NULL,
            reference text,
            PRIMARY KEY (sale_id, seq)
        )
    """)


# ══════════════════════ Bảng vận hành ══════════════════════


def _operational_tables() -> None:
    op.execute("""
        -- ⚠️ CHỐT CHẶN IDEMPOTENCY CỦA TOÀN HỆ THỐNG (docs/12 §5).
        -- Không phân vùng: cần UNIQUE toàn cục trên `event_id`, và đây chính là lý do
        -- `point_ledger` được phép có PK phức hợp.
        -- Dọn dẹp: giữ tối thiểu bằng thời gian tồn đọng outbox tối đa có thể (nhiều ngày
        -- nếu một cửa hàng offline dài). Xóa sớm = mở lại cửa cho sự kiện lặp.
        CREATE TABLE processed_event (
            event_id    uuid PRIMARY KEY,
            received_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX processed_event_received_idx ON processed_event (received_at)")

    op.execute("""
        -- Sự kiện không xử lý được: sai `schema_version`, payload không hợp lệ.
        -- docs/12 §4: version chưa hỗ trợ thì ghi vào đây và cảnh báo, KHÔNG cố đoán ý nghĩa.
        CREATE TABLE dead_letter_event (
            event_id       uuid PRIMARY KEY,
            store_id       text,
            event_type     text,
            schema_version integer,
            payload        jsonb NOT NULL,
            error          text NOT NULL,
            received_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        -- FR-C10 — `GET /sync/status`. Nguồn cho metric `store_sync_lag_seconds` (docs/08 §5).
        CREATE TABLE store_sync_status (
            store_id      text PRIMARY KEY REFERENCES store (store_id),
            last_event_at timestamptz,
            lag_seconds   integer,
            status        text NOT NULL DEFAULT 'UNKNOWN'
                          CHECK (status IN ('OK', 'LAGGING', 'STALE', 'UNKNOWN')),
            updated_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        -- C03: hai cửa hàng cùng tạo khách cho một SĐT khi đang offline. Phát hiện tự động,
        -- gộp thủ công — gộp sai thì không hoàn tác được vì ledger là append-only.
        CREATE TABLE customer_duplicate_candidate (
            phone_hash   text PRIMARY KEY,
            customer_ids uuid[] NOT NULL,
            detected_at  timestamptz NOT NULL DEFAULT now(),
            resolved_at  timestamptz
        )
    """)
    op.execute("""
        -- Watermark cho job đối soát TĂNG DẦN (ràng buộc #8). Quét toàn bộ không khả thi
        -- ở T3, nên job phải tăng dần NGAY TỪ BẢN ĐẦU — thêm sau là viết lại.
        CREATE TABLE reconciliation_watermark (
            job_name       text PRIMARY KEY,
            last_event_at  timestamptz,
            last_run_at    timestamptz,
            mismatch_count integer NOT NULL DEFAULT 0
        )
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS point_ledger_partition_health")
    op.execute("DROP FUNCTION IF EXISTS ensure_point_ledger_partitions(integer)")
    op.execute("""
        DROP TABLE IF EXISTS reconciliation_watermark, customer_duplicate_candidate,
                             store_sync_status, dead_letter_event, processed_event,
                             sale_payment_replica, sale_line_replica, sale_replica,
                             shift_replica, point_balance, point_ledger, customer,
                             employee, earn_rule, tier_rule, promotion, warehouse,
                             product_price_history, product, category, store, region CASCADE
    """)
