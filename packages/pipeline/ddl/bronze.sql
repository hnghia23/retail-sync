-- Bronze trong ClickHouse — chặng S5 của docs/17-data-flow.md.
--
-- Mỗi bảng nhận nguyên văn một file Parquet trích xuất từ Postgres TRUNG TÂM
-- (s3://lake/bronze/central/<bảng>/dt=<ngày nạp>/<start>_<end>.parquet). Không biến đổi ở
-- đây — làm sạch và khử trùng theo khóa nghiệp vụ là việc của dbt staging (silver, docs/17 §2).
--
-- ═══ Bẫy 4 (docs/17 §4) — ĐỌC TRƯỚC KHI THÊM BẢNG ═══
--
-- `insert_deduplication_token` bị BỎ QUA ÂM THẦM trên MergeTree một node nếu bảng không khai
-- `non_replicated_deduplication_window` (mặc định 0). Đã kiểm trên đúng image của compose:
-- chạy lại cùng một lần nạp → dữ liệu nhân đôi, không có lỗi nào
-- (tests/integration/test_clickhouse_bronze.py). MỌI bảng bronze phải có dòng SETTINGS đó —
-- test kiểm từng bảng trong file này, thêm bảng quên nó thì test đỏ.
--
-- Ba quy tắc cho bộ nạp, đều rút ra từ test chứ không từ tài liệu:
--   1. Token = đường dẫn file + SHA-256 nội dung. Cùng token mà khác nội dung thì ClickHouse
--      BỎ lần sau không báo gì — sửa một file rồi nạp lại với token cũ là mất bản sửa.
--   2. Cửa sổ khử trùng có hạn (100 000 khối gần nhất mỗi bảng). Nạp lại một file CŨ HƠN
--      cửa sổ đó sẽ nhân đôi. Chạy lại/backfill quá xa → đi đường số 3.
--   3. Dựng lại = `ALTER TABLE ... DROP PARTITION <tháng _dt>` rồi nạp lại các file của tháng
--      đó. Sau DROP PARTITION, nạp lại cùng token vẫn vào được (đã kiểm).
--
-- Quy ước cột:
--   `_dt`          ngày NẠP (phần `dt=` của đường dẫn) — ràng buộc #3, phân vùng theo cột này
--   `_source_file` đường dẫn file gốc — truy ngược một dòng về đúng lần trích xuất
--   `recorded_at`  mốc watermark ở trung tâm; một thực thể có thể có NHIỀU phiên bản (dòng
--                  bị UPDATE được trích xuất lại), staging lấy bản có `recorded_at` lớn nhất
--
-- Không PII (ràng buộc #10): `customer` KHÔNG mang `phone_hash`, `phone_enc`, `name_enc`.
-- Dò trùng khách (C03) chạy ở Postgres trung tâm, warehouse không cần SĐT dưới dạng nào.

CREATE TABLE IF NOT EXISTS bronze_sale
(
    sale_id                   UUID,
    store_id                  String,
    shift_id                  Nullable(UUID),
    business_date             Date32,
    employee_id               String,
    customer_id               Nullable(UUID),
    occurred_at               DateTime64(6, 'UTC'),
    recorded_at               DateTime64(6, 'UTC'),
    subtotal                  Int64,
    discount_tier             Int64,
    discount_promo            Int64,
    total                     Int64,
    tendered_amount           Nullable(Int64),
    change_amount             Nullable(Int64),
    promotion_id              Nullable(String),
    authorized_by_employee_id Nullable(String),
    status                    LowCardinality(String),
    voided_by_sale_id         Nullable(UUID),
    original_sale_id          Nullable(UUID),
    _dt                       Date,
    _source_file              String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (store_id, sale_id, recorded_at)
SETTINGS non_replicated_deduplication_window = 100000;

-- Dòng con đi cùng cửa sổ với cha (ghi trong cùng SAVEPOINT ở trung tâm); `recorded_at` là
-- của `sale_replica` cha, để staging chọn đúng phiên bản.
CREATE TABLE IF NOT EXISTS bronze_sale_line
(
    sale_id               UUID,
    line_no               Int32,
    product_id            String,
    quantity              Int32,
    unit_price            Int64,
    line_total            Int64,
    original_sale_id      Nullable(UUID),
    original_sale_line_no Nullable(Int32),
    recorded_at           DateTime64(6, 'UTC'),
    _dt                   Date,
    _source_file          String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (sale_id, line_no, recorded_at)
SETTINGS non_replicated_deduplication_window = 100000;

CREATE TABLE IF NOT EXISTS bronze_sale_payment
(
    sale_id      UUID,
    seq          Int32,
    method       LowCardinality(String),
    amount       Int64,
    reference    Nullable(String),
    recorded_at  DateTime64(6, 'UTC'),
    _dt          Date,
    _source_file String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (sale_id, seq, recorded_at)
SETTINGS non_replicated_deduplication_window = 100000;

CREATE TABLE IF NOT EXISTS bronze_point_ledger
(
    event_id     UUID,
    customer_id  UUID,
    store_id     String,
    sale_id      Nullable(UUID),
    delta        Int32,
    reason       LowCardinality(String),
    occurred_at  DateTime64(6, 'UTC'),
    recorded_at  DateTime64(6, 'UTC'),
    _dt          Date,
    _source_file String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (customer_id, event_id)
SETTINGS non_replicated_deduplication_window = 100000;

-- `variance_note` cố ý KHÔNG có: chữ tự do do quản lý gõ, không phục vụ phân tích nào, và là
-- chỗ PII dễ lọt vào nhất ("khách Nguyễn Văn A trả thiếu...").
CREATE TABLE IF NOT EXISTS bronze_shift
(
    shift_id              UUID,
    store_id              String,
    business_date         Date32,
    opened_by_employee_id String,
    closed_by_employee_id Nullable(String),
    opened_at             DateTime64(6, 'UTC'),
    closed_at             Nullable(DateTime64(6, 'UTC')),
    opening_cash          Int64,
    expected_cash         Nullable(Int64),
    counted_cash          Nullable(Int64),
    variance              Nullable(Int64),
    recorded_at           DateTime64(6, 'UTC'),
    _dt                   Date,
    _source_file          String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (store_id, shift_id, recorded_at)
SETTINGS non_replicated_deduplication_window = 100000;

CREATE TABLE IF NOT EXISTS bronze_customer
(
    customer_id  UUID,
    joined_at    DateTime64(6, 'UTC'),
    status       LowCardinality(String),
    merged_into  Nullable(UUID),
    recorded_at  DateTime64(6, 'UTC'),
    _dt          Date,
    _source_file String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (customer_id, recorded_at)
SETTINGS non_replicated_deduplication_window = 100000;

-- Watermark của bảng này là `updated_at` (docs/17 §2), giữ đúng tên cột nguồn.
CREATE TABLE IF NOT EXISTS bronze_point_balance
(
    customer_id     UUID,
    balance         Int32,
    lifetime_earned Int32,
    tier            LowCardinality(String),
    last_event_at   Nullable(DateTime64(6, 'UTC')),
    updated_at      DateTime64(6, 'UTC'),
    _dt             Date,
    _source_file    String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (customer_id, updated_at)
SETTINGS non_replicated_deduplication_window = 100000;

-- ═══ Snapshot master data — một file mỗi ngày (`dt=<ngày>/snapshot.parquet`) ═══
-- Nhỏ (< 10 nghìn dòng), nên chụp nguyên bảng thay vì theo dõi thay đổi. dbt lấy `_dt` mới nhất.

CREATE TABLE IF NOT EXISTS bronze_region
(
    region_id        String,
    name             String,
    parent_region_id Nullable(String),
    _dt              Date,
    _source_file     String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (region_id, _dt)
SETTINGS non_replicated_deduplication_window = 100000;

CREATE TABLE IF NOT EXISTS bronze_store
(
    store_id     String,
    name         String,
    city         Nullable(String),
    region_id    Nullable(String),
    status       LowCardinality(String),
    opened_at    Nullable(DateTime64(6, 'UTC')),
    closed_at    Nullable(DateTime64(6, 'UTC')),
    _dt          Date,
    _source_file String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (store_id, _dt)
SETTINGS non_replicated_deduplication_window = 100000;

CREATE TABLE IF NOT EXISTS bronze_category
(
    category_id        String,
    name               String,
    parent_category_id Nullable(String),
    _dt                Date,
    _source_file       String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (category_id, _dt)
SETTINGS non_replicated_deduplication_window = 100000;

CREATE TABLE IF NOT EXISTS bronze_product
(
    product_id   String,
    sku          String,
    barcode      Nullable(String),
    name         String,
    category_id  Nullable(String),
    unit_price   Int64,
    is_sellable  Bool,
    _dt          Date,
    _source_file String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (product_id, _dt)
SETTINGS non_replicated_deduplication_window = 100000;

-- Không `password_hash`: dữ liệu xác thực không có lý do nào để vào kho phân tích.
CREATE TABLE IF NOT EXISTS bronze_employee
(
    employee_id  String,
    store_id     Nullable(String),
    name         String,
    role         LowCardinality(String),
    status       LowCardinality(String),
    _dt          Date,
    _source_file String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (employee_id, _dt)
SETTINGS non_replicated_deduplication_window = 100000;

CREATE TABLE IF NOT EXISTS bronze_tier_rule
(
    tier                String,
    min_lifetime_points Int32,
    discount_pct        Int32,
    _dt                 Date,
    _source_file        String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_dt)
ORDER BY (tier, _dt)
SETTINGS non_replicated_deduplication_window = 100000;

-- ═══ Nhật ký nạp — một dòng mỗi file đã nạp ═══
-- Nguồn của DI-4 (số dòng bronze theo file = `rows`) và của mốc "đã nạp tới đâu" cho bộ đối
-- soát L3. Ghi SAU lần insert dữ liệu, cùng token: chết giữa hai bước thì chạy lại insert dữ
-- liệu bị khử trùng, còn dòng nhật ký được ghi bù.
CREATE TABLE IF NOT EXISTS bronze_load_log
(
    table_name   LowCardinality(String),
    path         String,
    sha256       String,
    rows         UInt64,
    window_start Nullable(DateTime64(6, 'UTC')),
    window_end   Nullable(DateTime64(6, 'UTC')),
    loaded_at    DateTime64(6, 'UTC') DEFAULT now64(6)
)
ENGINE = MergeTree
ORDER BY (table_name, path)
SETTINGS non_replicated_deduplication_window = 100000;
