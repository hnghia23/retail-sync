"""Schema cửa hàng — docs/05-data-model.md §3

Revision ID: 0001_edge_initial
Revises:
Create Date: 2026-09-17

Viết tay, không autogenerate: schema này có CHECK constraint, constraint trigger hoãn,
partial index và tham số autovacuum riêng — autogenerate không sinh ra được thứ nào
trong đó, mà chính chúng mới là phần giữ cho dữ liệu đúng.

Ba quyết định cần biết trước khi đọc:

1. `uuidv7()` là hàm native của PG 18 (docs/05 §1). Nếu spike S2 trượt, đổi DEFAULT sang
   sinh ở tầng Python (`shared.types.new_event_id`) — chỉ sửa DEFAULT, không đổi kiểu cột.

2. `CHECK (total >= 0)` ở docs/05 §3.3 KHÔNG áp được cho giao dịch trả hàng. Trả hàng là
   một `sale` mới với `quantity` âm (docs/05 §3.3) và `sale_payment.amount <> 0` — dấu
   `<> 0` thay vì `> 0` cho thấy hoàn tiền âm là có chủ đích — nên `subtotal`/`total` của
   nó âm. Ràng buộc ở đây là `status = 'RETURN' OR total >= 0`: giữ nguyên tinh thần "đơn
   bán không bao giờ âm" mà không chặn luồng trả hàng.

3. INV-2 và INV-3 là bất biến LIÊN DÒNG nên phải dùng CONSTRAINT TRIGGER
   DEFERRABLE INITIALLY DEFERRED: tại lúc `INSERT INTO sale` thì chưa có dòng nào của
   `sale_line`/`sale_payment`. Chỉ cuối transaction mới là thời điểm kiểm đúng.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_edge_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _master_data_cache()
    _shift()
    _sales()
    _customer_and_points()
    _stock_and_outbox()
    _invariant_triggers()
    _operational_indexes()


# ══════════════════════ 3.1 — Bản sao master data (đọc-only) ══════════════════════


def _master_data_cache() -> None:
    """Cửa hàng chỉ ĐỌC những bảng này; nguồn sự thật ở trung tâm (docs/05 §2).

    `synced_version` cho phép kéo delta thay vì kéo toàn bộ (FR-C01).
    """
    op.execute("""
        CREATE TABLE product_cache (
            product_id     text PRIMARY KEY,
            sku            text NOT NULL,
            barcode        text,
            name           text NOT NULL,
            category_id    text,
            unit_price     bigint NOT NULL CHECK (unit_price >= 0),
            -- G7: hai cờ khác nhau. Hàng ngừng kinh doanh vẫn BÁN được phần tồn trên kệ,
            -- nhưng không ĐẶT thêm được. Gộp một cờ là mất doanh thu hàng tồn.
            is_sellable    boolean NOT NULL DEFAULT true,
            is_orderable   boolean NOT NULL DEFAULT true,
            synced_version bigint NOT NULL DEFAULT 0,
            synced_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE promotion_cache (
            promotion_id   text PRIMARY KEY,
            name           text NOT NULL,
            rule           jsonb NOT NULL,
            valid_from     timestamptz NOT NULL,
            valid_to       timestamptz,
            synced_version bigint NOT NULL DEFAULT 0,
            synced_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX promotion_cache_validity_idx ON promotion_cache (valid_from, valid_to)"
    )

    op.execute("""
        CREATE TABLE tier_rule_cache (
            tier                text PRIMARY KEY,
            min_lifetime_points integer NOT NULL CHECK (min_lifetime_points >= 0),
            discount_pct        integer NOT NULL CHECK (discount_pct BETWEEN 0 AND 100),
            synced_version      bigint NOT NULL DEFAULT 0,
            synced_at           timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE earn_rule_cache (
            rule_id        text PRIMARY KEY,
            vnd_per_point  bigint NOT NULL CHECK (vnd_per_point > 0),
            multiplier     numeric(6,3) NOT NULL DEFAULT 1.0 CHECK (multiplier >= 0),
            valid_from     timestamptz NOT NULL,
            valid_to       timestamptz,
            synced_version bigint NOT NULL DEFAULT 0,
            synced_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX earn_rule_cache_validity_idx ON earn_rule_cache (valid_from, valid_to)"
    )

    op.execute("""
        CREATE TABLE employee_cache (
            employee_id    text PRIMARY KEY,
            name           text NOT NULL,
            role           text NOT NULL CHECK (role IN ('cashier', 'manager', 'region_manager', 'admin')),
            password_hash  text NOT NULL,
            is_active      boolean NOT NULL DEFAULT true,
            -- S01: thu hồi quyền phải có hiệu lực CẢ KHI cửa hàng đang offline, nên cột
            -- này nằm ở bản sao cục bộ và được kiểm lúc đăng nhập — không hỏi trung tâm.
            revoked_at     timestamptz,
            synced_version bigint NOT NULL DEFAULT 0,
            synced_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        -- Watermark đồng bộ theo từng loại tài nguyên. `last_synced_at` là nguồn cho cảnh
        -- báo master data cũ (> 24h — B02 E03).
        CREATE TABLE sync_state (
            resource       text PRIMARY KEY,
            last_version   bigint NOT NULL DEFAULT 0,
            last_synced_at timestamptz
        )
    """)


# ══════════════════════ 3.2 — Ca làm việc ══════════════════════


def _shift() -> None:
    """G4 — v1 hoàn toàn thiếu khái niệm ca, nên không chốt ca được."""
    op.execute("""
        CREATE TABLE shift (
            shift_id      uuid PRIMARY KEY DEFAULT uuidv7(),
            store_id      text NOT NULL,
            -- G5: KHÔNG suy ra từ opened_at. Ca kéo qua nửa đêm (B02 Z03) — bán lúc 00:30
            -- vẫn thuộc ngày kinh doanh hôm trước.
            business_date date NOT NULL,
            opened_by_employee_id text NOT NULL,
            closed_by_employee_id text,
            opened_at     timestamptz NOT NULL DEFAULT now(),
            closed_at     timestamptz,
            opening_cash  bigint NOT NULL DEFAULT 0,
            expected_cash bigint,
            counted_cash  bigint,
            -- Chênh lệch được GHI NHẬN. Không bao giờ sửa giao dịch để bù nó về 0.
            variance      bigint,
            variance_note text,
            status        text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),

            CONSTRAINT shift_closed_has_closed_at
                CHECK (status = 'OPEN' OR closed_at IS NOT NULL)
        )
    """)
    op.execute("CREATE INDEX shift_store_business_date_idx ON shift (store_id, business_date)")
    op.execute("""
        -- Mỗi cửa hàng chỉ MỘT ca đang mở. Nếu không, `POST /sales` không biết ghi vào ca
        -- nào và báo cáo chốt ca sẽ thiếu đơn.
        CREATE UNIQUE INDEX shift_one_open_per_store_idx
            ON shift (store_id) WHERE status = 'OPEN'
    """)


# ══════════════════════ 3.3 — Giao dịch ══════════════════════


def _sales() -> None:
    op.execute("""
        CREATE TABLE sale (
            sale_id       uuid PRIMARY KEY DEFAULT uuidv7(),
            -- v1 THIẾU cột này → trung tâm hardcode store_key = 1 và mọi phân tích theo
            -- cửa hàng sụp đổ. Bắt buộc có trong MỌI bảng giao dịch.
            store_id      text NOT NULL,
            shift_id      uuid NOT NULL REFERENCES shift (shift_id),
            business_date date NOT NULL,
            employee_id   text NOT NULL,
            customer_id   uuid,
            occurred_at   timestamptz NOT NULL DEFAULT now(),

            subtotal        bigint NOT NULL,
            discount_tier   bigint NOT NULL DEFAULT 0,
            discount_promo  bigint NOT NULL DEFAULT 0,
            total           bigint NOT NULL,
            tendered_amount bigint,
            change_amount   bigint,

            promotion_id  text,
            authorized_by_employee_id text,

            status        text NOT NULL DEFAULT 'COMPLETED'
                          CHECK (status IN ('COMPLETED', 'VOIDED', 'RETURN')),
            voided_by_sale_id uuid REFERENCES sale (sale_id),
            original_sale_id  uuid REFERENCES sale (sale_id),

            -- ⚠️ INV-1, bất biến kế toán. Làm tròn sai KHÔNG phải lệch số — giao dịch bị
            -- TỪ CHỐI. Đây là hệ quả cấu trúc của PRICING_ROUNDING (docs/05 §5).
            CONSTRAINT sale_inv1_accounting
                CHECK (subtotal = total + discount_tier + discount_promo),

            -- Đơn bán không bao giờ âm; đơn TRẢ thì âm là đúng (xem docstring module).
            CONSTRAINT sale_total_sign
                CHECK (status = 'RETURN' OR total >= 0),

            CONSTRAINT sale_return_has_original
                CHECK (status <> 'RETURN' OR original_sale_id IS NOT NULL)
        )
    """)

    op.execute("""
        CREATE TABLE sale_line (
            sale_id    uuid NOT NULL REFERENCES sale (sale_id) ON DELETE CASCADE,
            line_no    integer NOT NULL,
            product_id text NOT NULL,
            quantity   integer NOT NULL CHECK (quantity <> 0),
            -- ⚠️ ĐÓNG BĂNG giá tại thời điểm bán. Không bao giờ join lại `product_cache`
            -- để lấy giá: giá đổi thì hóa đơn cũ đổi theo — sai cả kế toán lẫn pháp lý.
            unit_price bigint NOT NULL CHECK (unit_price >= 0),
            line_total bigint NOT NULL,

            -- G3: trả hàng một phần — trỏ về đúng dòng gốc để kiểm INV-5
            original_sale_id      uuid,
            original_sale_line_no integer,

            PRIMARY KEY (sale_id, line_no),
            CONSTRAINT sale_line_original_ref
                FOREIGN KEY (original_sale_id, original_sale_line_no)
                REFERENCES sale_line (sale_id, line_no),
            CONSTRAINT sale_line_original_pair
                CHECK ((original_sale_id IS NULL) = (original_sale_line_no IS NULL))
        )
    """)
    op.execute("CREATE INDEX sale_line_product_idx ON sale_line (product_id)")
    op.execute("""
        CREATE INDEX sale_line_original_idx
            ON sale_line (original_sale_id, original_sale_line_no)
            WHERE original_sale_id IS NOT NULL
    """)

    op.execute("""
        -- G1: MỘT đơn, NHIỀU hình thức thanh toán. v1 chỉ có một cột `payment_method`.
        CREATE TABLE sale_payment (
            sale_id   uuid NOT NULL REFERENCES sale (sale_id) ON DELETE CASCADE,
            seq       integer NOT NULL,
            method    text NOT NULL CHECK (method IN ('CASH', 'CARD', 'EWALLET')),
            -- Âm = hoàn tiền khi trả hàng.
            amount    bigint NOT NULL CHECK (amount <> 0),
            reference text,
            PRIMARY KEY (sale_id, seq)
        )
    """)


# ══════════════════════ 3.4 — Khách hàng & sổ cái điểm cục bộ ══════════════════════


def _customer_and_points() -> None:
    """Ràng buộc #10: không PII đọc được. `phone_hash` để tra, `*_enc` để hiển thị."""
    op.execute("""
        CREATE TABLE customer_local (
            customer_id     uuid PRIMARY KEY,
            phone_hash      text NOT NULL,
            phone_enc       bytea,
            name_enc        bytea,
            tier            text NOT NULL DEFAULT 'BRONZE',
            -- "cached_*" là gợi ý hiển thị, KHÔNG phải nguồn sự thật. Nguồn sự thật là
            -- ledger trung tâm (docs/03 §4.3).
            cached_balance         integer NOT NULL DEFAULT 0,
            cached_lifetime_earned integer NOT NULL DEFAULT 0,
            cached_at              timestamptz,
            -- C02: tạo khách khi offline. Trung tâm dùng cờ này để dò trùng (C03).
            created_locally boolean NOT NULL DEFAULT false,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX customer_local_phone_hash_idx ON customer_local (phone_hash)")

    op.execute("""
        -- ADR-002: APPEND-ONLY. Không bao giờ UPDATE, không bao giờ DELETE.
        -- Số dư là kết quả suy ra, không phải một cột được sửa.
        CREATE TABLE point_ledger_local (
            event_id    uuid PRIMARY KEY DEFAULT uuidv7(),
            customer_id uuid NOT NULL,
            store_id    text NOT NULL,
            sale_id     uuid REFERENCES sale (sale_id),
            delta       integer NOT NULL CHECK (delta <> 0),
            reason      text NOT NULL
                        CHECK (reason IN ('EARN', 'REDEEM', 'RETURN', 'EXPIRE', 'ADJUST')),
            occurred_at timestamptz NOT NULL DEFAULT now(),
            -- NULL = chưa lên trung tâm. Dùng cho công thức tránh số dư tụt lùi:
            -- hiển thị = số dư trung tâm + SUM(ledger cục bộ chưa gửi)  (docs/03 §4.3).
            synced_at   timestamptz
        )
    """)
    op.execute("""
        CREATE INDEX point_ledger_local_customer_idx
            ON point_ledger_local (customer_id, occurred_at DESC)
    """)
    op.execute("""
        CREATE INDEX point_ledger_local_unsynced_idx
            ON point_ledger_local (customer_id) WHERE synced_at IS NULL
    """)


# ══════════════════════ 3.5 — Tồn kho & Outbox ══════════════════════


def _stock_and_outbox() -> None:
    op.execute("""
        CREATE TABLE stock (
            product_id   text PRIMARY KEY,
            qty_shelf    integer NOT NULL DEFAULT 0,
            qty_backroom integer NOT NULL DEFAULT 0,
            updated_at   timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE outbox (
            id         bigserial PRIMARY KEY,
            event_id   uuid NOT NULL UNIQUE DEFAULT uuidv7(),
            event_type text NOT NULL,
            -- Chứa cả envelope lẫn `trace.traceparent` (ràng buộc #7): độ trễ store→central
            -- có thể hàng giờ, nên trace context đi cùng DỮ LIỆU, không đi cùng HTTP header.
            payload    jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            sent_at    timestamptz,
            attempts   integer NOT NULL DEFAULT 0,
            last_error text,
            -- Sự kiện độc không được chặn hàng đợi vĩnh viễn (ADR-003).
            dead_lettered_at timestamptz
        )
    """)
    op.execute("""
        -- Partial index: chỉ index dòng CHƯA gửi. Bảng 10 triệu dòng thì index vẫn chỉ
        -- bằng số dòng đang tồn đọng — thường là vài chục.
        CREATE INDEX outbox_unsent_idx ON outbox (id)
            WHERE sent_at IS NULL AND dead_lettered_at IS NULL
    """)
    op.execute("""
        CREATE INDEX outbox_dead_letter_idx ON outbox (created_at)
            WHERE dead_lettered_at IS NOT NULL
    """)
    # ⚠️ BẮT BUỘC (ràng buộc #9) — outbox là bảng DUY NHẤT có mẫu insert → update → delete,
    # tức bảng duy nhất sinh bloat. Autovacuum mặc định (scale_factor 0.2) quá thưa: bảng
    # phải phình 20% mới được dọn, mà 20% của một bảng lớn là rất nhiều dead tuple.
    op.execute("""
        ALTER TABLE outbox SET (
            autovacuum_vacuum_scale_factor  = 0.02,
            autovacuum_vacuum_threshold     = 50,
            autovacuum_analyze_scale_factor = 0.05
        )
    """)


# ══════════════════════ §6 — Bất biến liên dòng ══════════════════════


def _invariant_triggers() -> None:
    """INV-2 và INV-3 — docs/05 §6.

    Nguyên tắc: bất biến trong một dòng → CHECK. Liên dòng → trigger. Liên hệ thống → job
    đối soát. Không bao giờ chỉ dựa vào tầng ứng dụng.

    `DEFERRABLE INITIALLY DEFERRED` là bắt buộc chứ không phải tùy chọn: luồng chốt đơn ghi
    `sale` trước rồi mới ghi `sale_line`/`sale_payment` trong cùng transaction. Trigger
    không hoãn sẽ nổ ngay ở dòng `sale` đầu tiên, dù dữ liệu hoàn toàn đúng.
    """
    op.execute("""
        CREATE FUNCTION check_sale_line_sum() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        DECLARE
            v_sale_id  uuid := COALESCE(NEW.sale_id, OLD.sale_id);
            v_subtotal bigint;
            v_sum      bigint;
        BEGIN
            SELECT subtotal INTO v_subtotal FROM sale WHERE sale_id = v_sale_id;
            IF NOT FOUND THEN
                RETURN NULL;   -- đơn đã bị xóa trong cùng transaction: không còn gì để kiểm
            END IF;

            SELECT COALESCE(sum(line_total), 0) INTO v_sum
            FROM sale_line WHERE sale_id = v_sale_id;

            IF v_sum <> v_subtotal THEN
                RAISE EXCEPTION
                    'INV-3 vi pham: SUM(sale_line.line_total)=% <> sale.subtotal=% (sale_id=%)',
                    v_sum, v_subtotal, v_sale_id;
            END IF;
            RETURN NULL;
        END $fn$
    """)
    op.execute("""
        CREATE FUNCTION check_sale_payment_sum() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        DECLARE
            v_sale_id uuid := COALESCE(NEW.sale_id, OLD.sale_id);
            v_total   bigint;
            v_sum     bigint;
        BEGIN
            SELECT total INTO v_total FROM sale WHERE sale_id = v_sale_id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;

            SELECT COALESCE(sum(amount), 0) INTO v_sum
            FROM sale_payment WHERE sale_id = v_sale_id;

            IF v_sum <> v_total THEN
                RAISE EXCEPTION
                    'INV-2 vi pham: SUM(sale_payment.amount)=% <> sale.total=% (sale_id=%)',
                    v_sum, v_total, v_sale_id;
            END IF;
            RETURN NULL;
        END $fn$
    """)
    # Trên `sale`: bắt đơn không có dòng hàng / không có thanh toán.
    op.execute("""
        CREATE CONSTRAINT TRIGGER sale_inv3_line_sum
            AFTER INSERT OR UPDATE ON sale
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_sale_line_sum()
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER sale_inv2_payment_sum
            AFTER INSERT OR UPDATE ON sale
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_sale_payment_sum()
    """)
    # Trên bảng con: bắt việc sửa/xóa dòng làm lệch tổng.
    op.execute("""
        CREATE CONSTRAINT TRIGGER sale_line_inv3
            AFTER INSERT OR UPDATE OR DELETE ON sale_line
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_sale_line_sum()
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER sale_payment_inv2
            AFTER INSERT OR UPDATE OR DELETE ON sale_payment
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_sale_payment_sum()
    """)


# ══════════════════════ 3.6 — Index cho báo cáo vận hành ══════════════════════


def _operational_indexes() -> None:
    """Lớp truy vấn A (docs/03 §5b) chạy trên chính DB này và phải nhanh KỂ CẢ KHI OFFLINE.

    Bốn index này là toàn bộ những gì cần — thêm nữa chỉ tốn chi phí ghi.
    """
    op.execute("CREATE INDEX sale_shift_idx ON sale (shift_id)")
    op.execute("CREATE INDEX sale_store_date_idx ON sale (store_id, business_date)")
    op.execute("CREATE INDEX sale_customer_recent_idx ON sale (customer_id, occurred_at DESC)")
    op.execute("""
        CREATE UNIQUE INDEX product_cache_barcode_idx ON product_cache (barcode)
            WHERE barcode IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS outbox, stock, point_ledger_local, customer_local,
                             sale_payment, sale_line, sale, shift, sync_state,
                             employee_cache, earn_rule_cache, tier_rule_cache,
                             promotion_cache, product_cache CASCADE
    """)
    op.execute("DROP FUNCTION IF EXISTS check_sale_line_sum()")
    op.execute("DROP FUNCTION IF EXISTS check_sale_payment_sum()")
