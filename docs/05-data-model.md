# 05 — Mô hình dữ liệu

> **Trạng thái:** ✅ Đã gộp toàn bộ phát hiện từ [business/](business/) (8 lỗ hổng schema) và
> các quyết định trong [ADR-001…009](adr/). **Sẵn sàng cho giai đoạn thiết kế chi tiết.**
>
> Đây là mô hình **khái niệm và cấu trúc**. DDL chính thức sẽ nằm trong migration Alembic;
> doc này là nguồn sự thật cho *hình dạng* dữ liệu và *vì sao* nó như vậy.

---

## 1. Quyết định về định danh

| Quyết định | Giá trị | Lý do |
|---|---|---|
| ID giao dịch / sự kiện | **UUIDv7** — `uuidv7()` native của PG 18 | v1 dùng `INT AUTO_INCREMENT` → **đụng ID khi gộp về trung tâm**. UUIDv4 thì phá index. UUIDv7 duy nhất toàn cục **và** sắp xếp theo thời gian: insert nhanh hơn ~10×, index nhỏ hơn ~25% |
| `store_id` | Có mặt trong **mọi** bảng giao dịch | v1 thiếu → trung tâm hardcode `store_key = 1`, mọi phân tích theo cửa hàng sụp đổ |
| ID khách hàng | `customer_id` UUID nội bộ; SĐT là **định danh đăng nhập** riêng | Đổi SĐT được, gộp tài khoản được, mask PII trong warehouse dễ |
| Tiền tệ | `bigint`, đơn vị **đồng** | **Không bao giờ float cho tiền** |
| Thời gian | `timestamptz` lưu UTC | Phân biệt `occurred_at` (tại cửa hàng) vs `recorded_at` (tại trung tâm) — cần cho phát hiện lệch đồng hồ |
| **Ngày kinh doanh** | `business_date date` tách khỏi ngày lịch | Ca làm việc kéo qua nửa đêm ([B02 Z03](business/02-edge-cases.md)) |
| Multi-tenant | **Không có `tenant_id`** | Dùng database-per-tenant nếu cần ([ADR-008 C5](adr/008-remaining-decisions.md)) |

## 2. Sở hữu dữ liệu

| Dữ liệu | Nguồn sự thật | Bản sao |
|---|---|---|
| Sản phẩm, giá, khuyến mãi, quy tắc hạng/tích điểm, nhân viên | **Trung tâm** | Cửa hàng đọc-only, kéo theo phiên bản |
| Hồ sơ khách hàng | **Trung tâm** | Cửa hàng cache; tạo mới được khi offline |
| **Sổ cái điểm** | **Trung tâm** (hợp nhất) | Cửa hàng ghi cục bộ rồi đẩy lên |
| **Đơn hàng, ca làm việc, tồn kho** | **Cửa hàng** | Trung tâm nhận bản sao để phân tích |

Rút gọn: **trung tâm sở hữu master data, cửa hàng sở hữu giao dịch, điểm thì hợp nhất.**

---

## 3. Schema cửa hàng (PostgreSQL 18)

### 3.1. Bản sao master data (đọc-only)

```sql
product_cache(product_id, sku, barcode, name, category_id, unit_price,
              is_sellable bool,          -- G7: bán được (kể cả hàng ngừng kinh doanh còn tồn)
              is_orderable bool,         -- G7: còn đặt hàng nhà cung cấp được
              synced_version, synced_at)
promotion_cache(promotion_id, name, rule jsonb, valid_from, valid_to, synced_version)
tier_rule_cache(tier, min_lifetime_points, discount_pct, synced_version)
earn_rule_cache(rule_id, vnd_per_point, multiplier, valid_from, valid_to, synced_version)
employee_cache(employee_id, name, role, password_hash, is_active,
               revoked_at,               -- S01: thu hồi quyền khi cửa hàng offline
               synced_version)
sync_state(resource PK, last_version, last_synced_at)
```

> ⚠️ **Cảnh báo `synced_at`:** nếu cửa hàng offline lâu, master data cũ dần. Cần cảnh báo khi
> `now() - synced_at > 24h` ([B02 E03](business/02-edge-cases.md)).

### 3.2. Ca làm việc 🆕 *(G4 — nghiệp vụ hằng ngày, v1 hoàn toàn thiếu)*

```sql
CREATE TABLE shift (
    shift_id      uuid PRIMARY KEY DEFAULT uuidv7(),
    store_id      text NOT NULL,
    business_date date NOT NULL,                    -- G5: KHÔNG suy ra từ opened_at
    opened_by_employee_id text NOT NULL,
    closed_by_employee_id text,
    opened_at     timestamptz NOT NULL,
    closed_at     timestamptz,
    opening_cash  bigint NOT NULL DEFAULT 0,
    expected_cash bigint,        -- hệ thống tính: opening + tiền mặt thu − chi
    counted_cash  bigint,        -- thực đếm
    variance      bigint,        -- chênh lệch — KHÔNG sửa giao dịch để bù
    variance_note text,
    status        text NOT NULL DEFAULT 'OPEN'      -- OPEN | CLOSED
);
CREATE INDEX ON shift (store_id, business_date);
```

### 3.3. Giao dịch

```sql
CREATE TABLE sale (
    sale_id       uuid PRIMARY KEY DEFAULT uuidv7(),
    store_id      text NOT NULL,                    -- ← v1 THIẾU
    shift_id      uuid NOT NULL REFERENCES shift(shift_id),
    business_date date NOT NULL,
    employee_id   text NOT NULL,
    customer_id   uuid,                             -- NULL = khách vãng lai
    occurred_at   timestamptz NOT NULL,

    subtotal        bigint NOT NULL,                -- tạm tính, trước giảm giá
    discount_tier   bigint NOT NULL DEFAULT 0,
    discount_promo  bigint NOT NULL DEFAULT 0,
    total           bigint NOT NULL,
    tendered_amount bigint,                         -- G2: tiền khách đưa
    change_amount   bigint,                         -- G2: tiền thối

    promotion_id  text,                             -- G8: khuyến mãi đã áp
    authorized_by_employee_id text,                 -- G6: ai duyệt chiết khấu vượt ngưỡng

    status        text NOT NULL DEFAULT 'COMPLETED',-- COMPLETED | VOIDED | RETURN
    voided_by_sale_id uuid,                         -- trỏ tới giao dịch đối ứng

    -- ⚠️ KHÔNG phải `CHECK (total >= 0)` trần: đơn TRẢ HÀNG có quantity âm, line_total âm
    -- và sale_payment.amount âm (hoàn tiền), nên total của nó âm là đúng.
    CHECK (status = 'RETURN' OR total >= 0),
    CHECK (subtotal = total + discount_tier + discount_promo)   -- ⚠️ bất biến kế toán
);

CREATE TABLE sale_line (
    sale_id    uuid REFERENCES sale(sale_id),
    line_no    int,
    product_id text NOT NULL,
    quantity   int NOT NULL CHECK (quantity <> 0),  -- âm khi trả hàng
    unit_price bigint NOT NULL,                     -- ⚠️ ĐÓNG BĂNG giá tại thời điểm bán
    line_total bigint NOT NULL,
    -- G3: trả hàng một phần — trỏ về dòng gốc
    original_sale_id      uuid,
    original_sale_line_no int,
    PRIMARY KEY (sale_id, line_no)
);

-- G1: MỘT đơn, NHIỀU hình thức thanh toán  🆕
CREATE TABLE sale_payment (
    sale_id   uuid REFERENCES sale(sale_id),
    seq       int,
    method    text   NOT NULL,        -- CASH | CARD | EWALLET
    amount    bigint NOT NULL CHECK (amount <> 0),
    reference text,                   -- mã giao dịch thẻ/ví
    PRIMARY KEY (sale_id, seq)
);
```

### 3.4. Khách hàng & sổ cái điểm cục bộ

```sql
customer_local(customer_id uuid PK, phone_hash text, name_enc bytea,
               tier text, cached_balance int, cached_at timestamptz,
               created_locally bool DEFAULT false)   -- C02: tạo khi offline

point_ledger_local(
  event_id uuid PK DEFAULT uuidv7(),
  customer_id uuid, store_id text, sale_id uuid,
  delta int NOT NULL,
  reason text NOT NULL,                -- EARN | REDEEM | RETURN | EXPIRE | ADJUST
  occurred_at timestamptz NOT NULL,
  synced_at timestamptz                -- NULL = chưa gửi lên trung tâm
);
```

### 3.5. Tồn kho & Outbox

```sql
stock(product_id text PK, qty_shelf int, qty_backroom int, updated_at timestamptz);

CREATE TABLE outbox (
    id         bigserial PRIMARY KEY,
    event_id   uuid UNIQUE NOT NULL DEFAULT uuidv7(),
    event_type text NOT NULL,
    payload    jsonb NOT NULL,         -- chứa cả "_trace" (traceparent) — xem doc 10
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at    timestamptz,
    attempts   int DEFAULT 0,
    last_error text
);

-- Partial index: chỉ index dòng CHƯA gửi → index luôn nhỏ, dù bảng lớn
CREATE INDEX outbox_unsent_idx ON outbox (id) WHERE sent_at IS NULL;

-- ⚠️ BẮT BUỘC: outbox là bảng DUY NHẤT có mẫu sinh bloat (insert → update → delete)
ALTER TABLE outbox SET (
    autovacuum_vacuum_scale_factor  = 0.02,     -- mặc định 0.2 quá thưa
    autovacuum_vacuum_threshold     = 50,
    autovacuum_analyze_scale_factor = 0.05
);
```

### 3.6. Index phục vụ báo cáo vận hành 🆕

Lớp truy vấn A ([B03](business/03-analytical-workload.md)) chạy trên chính DB cửa hàng và
phải nhanh **kể cả khi offline**. Bốn index này là toàn bộ những gì cần:

```sql
CREATE INDEX ON sale (shift_id);                          -- báo cáo chốt ca
CREATE INDEX ON sale (store_id, business_date);           -- doanh thu hôm nay
CREATE INDEX ON sale (customer_id, occurred_at DESC);     -- tra đơn cũ để trả hàng (R03)
CREATE UNIQUE INDEX ON product_cache (barcode);           -- quét mã vạch
```

---

## 4. Schema trung tâm (PostgreSQL 18)

### 4.1. Master data

```sql
region(region_id PK, name, parent_region_id)              -- dataset còn thiếu ở v1
store(store_id PK, name, address, city, region_id, manager_employee_id,
      opened_at, closed_at, status, version)
category(category_id PK, name, parent_category_id)
product(product_id PK, sku, barcode, name, category_id, unit_price,
        is_sellable, is_orderable, version)
product_price_history(product_id, price, valid_from, valid_to)   -- nuôi SCD2
warehouse(warehouse_id PK, name, region_id, capacity)     -- dataset còn thiếu ở v1
promotion(promotion_id PK, name, rule jsonb, valid_from, valid_to, version)
tier_rule(tier PK, min_lifetime_points, discount_pct, version)
earn_rule(rule_id PK, vnd_per_point, multiplier, valid_from, valid_to)
employee(employee_id PK, store_id, name, role, password_hash,
         hired_at, left_at, revoked_at, status)
   -- ⚠️ KHÔNG có cột salary — dữ liệu HR, không thuộc hệ thống này
```

> **Quy tắc thời gian hiệu lực:** `promotion`, `tier_rule`, `earn_rule` đều có
> `valid_from`/`valid_to`. Giao dịch áp **quy tắc có hiệu lực tại thời điểm mở đơn**, không
> phải quy tắc hiện tại ([B02 E12, L05](business/02-edge-cases.md)).

### 4.2. Khách hàng

```sql
customer(customer_id uuid PK DEFAULT uuidv7(),
         phone_hash text,               -- index thường, KHÔNG UNIQUE — xem ghi chú
         phone_enc bytea, name_enc bytea,
         joined_at, status,
         merged_into uuid,              -- C03: gộp khách trùng
         recorded_at timestamptz)       -- watermark trích xuất — trigger set ở MỌI INSERT/UPDATE
```

> ✅ **`recorded_at` thêm ở migration `0004_central_extract_watermarks`** ([17 §4](17-data-flow.md)
> bẫy 3). `CustomerUpdated` sửa dòng tại chỗ, và trước đây bảng không có cột nào ghi lúc dòng
> đổi, nên trích xuất tăng dần không thấy bản cập nhật. Cột được **trigger** đặt bằng `now()` ở
> mọi `INSERT`/`UPDATE`, không phụ thuộc handler. Cùng trigger cũng gắn cho `sale_replica`,
> `shift_replica` (`recorded_at`) và `point_balance` (`updated_at`).

> **`joined_at`** = `occurred_at` của `CustomerCreated`, tức lúc đăng ký tại cửa hàng (sửa
> 2026-09-23). Bản đầu để `DEFAULT now()`, nên cửa hàng offline 3 ngày làm ngày gia nhập của
> khách lệch 3 ngày trong `dim_customer`.

> ⚠️ **`phone_hash` ở trung tâm không UNIQUE** (đổi ở migration `0003_central_ingest`,
> 2026-09-23). Bản đầu có UNIQUE, và nó làm chính case C03 không thể ghi nhận: khách thứ hai
> (tạo ở cửa hàng khác lúc offline) không vào được bảng, nên mọi `PointsEarned` của khách đó
> vỡ khóa ngoại — điểm thật mất khỏi trung tâm. Giờ cả hai bản ghi tồn tại, cặp trùng vào
> `customer_duplicate_candidate` để gộp thủ công. Phía **cửa hàng** giữ UNIQUE trên
> `customer_local.phone_hash`: trong một cửa hàng, một SĐT đúng là một khách.

### 4.3. Sổ cái điểm — nguồn sự thật ([ADR-002](adr/002-point-ledger.md))

```sql
CREATE TABLE point_ledger (
    event_id    uuid NOT NULL,           -- sinh tại cửa hàng = khóa idempotency
    customer_id uuid NOT NULL,
    store_id    text NOT NULL,
    sale_id     uuid,
    delta       int  NOT NULL,           -- + tích, − tiêu/trả/hết hạn
    reason      text NOT NULL,
    occurred_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    metadata    jsonb,
    PRIMARY KEY (event_id, occurred_at)  -- ⚠️ xem ghi chú bên dưới
) PARTITION BY RANGE (occurred_at);      -- ⚠️ phân vùng theo THÁNG ngay từ đầu

CREATE TABLE point_balance (             -- snapshot, dựng lại được từ ledger
    customer_id     uuid PRIMARY KEY,
    balance         int  NOT NULL,
    lifetime_earned int  NOT NULL,       -- ⚠️ xếp hạng dùng cột NÀY, không phải balance
    tier            text NOT NULL,
    last_event_at   timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
```

> ⚠️ **`PRIMARY KEY` buộc phải chứa cột phân vùng.** Postgres không cho phép PK chỉ gồm
> `event_id` trên bảng `PARTITION BY RANGE (occurred_at)`. Điều này **không** làm yếu
> idempotency: chốt chặn chống lặp là `processed_event(event_id)` — bảng không phân vùng,
> có UNIQUE toàn cục ([doc 12 §5](12-event-schema.md)) — còn `point_ledger` chỉ được ghi
> *sau khi* `claim_event()` trả về true.

> ✅ **Index `point_ledger_recorded_idx` trên `recorded_at`** (migration `0004`, [17 §4](17-data-flow.md)
> bẫy 2). Bảng phân vùng theo `occurred_at`, còn trích xuất bronze lọc theo `recorded_at`, nên
> không có index thì mỗi lần trích xuất quét mọi partition. Index trên bảng cha tự áp cho mọi
> partition, kể cả partition `ensure_point_ledger_partitions()` tạo sau này.
>
> Trích xuất tăng dần lấy mép cửa sổ bằng hàm **`extract_horizon()`** (cùng migration): mốc là
> transaction đang mở cũ nhất, để dòng commit muộn không lọt (bẫy 1).

> ⚠️ **Partition phải được tạo tự động trước 3 tháng.** Postgres không tự tạo partition tương
> lai — hết partition = **mọi INSERT lỗi** = toàn hệ thống điểm chết. Dùng `pg_partman` hoặc
> job, kèm kiểm tra hằng ngày. Đây là rủi ro sập cao nhất của thiết kế
> ([08 §4.1](08-reliability-and-scale.md)).
>
> ✅ **Và cả THÁNG TRƯỚC** (migration `0006`, 2026-09-24). Cửa hàng offline vắt qua cuối tháng,
> đồng bộ ngày 1 → điểm của tháng trước cần partition tháng trước. Khi hệ thống đã chạy vài
> tháng thì nó có sẵn; ở **tháng go-live và mọi môi trường mới** thì không, và trung tâm từ
> chối KHÔNG thử lại (23514) → điểm vào dead-letter, đơn thì vẫn vào. Phát hiện bằng bộ giả lập
> `virtual`. `ensure_point_ledger_partitions(months_ahead, months_back = 1)`; lùi xa hơn vẫn từ
> chối ồn ào (đồng hồ sai / dữ liệu quá cũ).

### 4.4. Bản sao giao dịch & bảng vận hành

```sql
sale_replica, sale_line_replica, sale_payment_replica, shift_replica   -- cùng hình dạng
store_sync_status(store_id PK, last_event_at, lag_seconds, status)
processed_event(event_id uuid PK, received_at)    -- chốt chặn idempotency
dead_letter_event(event_id PK, store_id, event_type, schema_version, payload, error)
customer_duplicate_candidate(phone_hash, customer_ids uuid[], detected_at)  -- C03
store_credential(store_id PK → store, key_sha256 UNIQUE, created_at, revoked_at)  -- docs/13 §2
```

`store_credential` lưu SHA-256 của khóa, không phải Argon2: khóa là 32 byte ngẫu nhiên do máy
sinh (không có không gian đoán cần làm chậm), và Argon2 tốn ~50ms mỗi lần kiểm — gần hết
ngân sách 300ms của route tra cứu.

---

## 5. Quy tắc nghiệp vụ — dữ liệu hay cấu hình?

Chủ dự án chỉ đạo: quy tắc nghiệp vụ **tùy chỉnh được theo thực tế**
([ADR-008](adr/008-remaining-decisions.md)). Phân biệt hai loại:

| Loại | Ví dụ | Lưu ở đâu | Vì sao |
|---|---|---|---|
| **Dữ liệu có hiệu lực theo thời gian** | Tỷ lệ tích điểm, ngưỡng hạng, khuyến mãi | **Bảng trong DB trung tâm**, đồng bộ xuống | Giao dịch cũ phải áp quy tắc cũ — cần lịch sử |
| **Hành vi hệ thống** | Cách làm tròn, cộng gộp chiết khấu, cho phép số dư âm | **Cấu hình ứng dụng** (pydantic-settings) | Áp cho mọi giao dịch mới; đổi hồi tố sẽ sai |

```python
class PricingRules(BaseSettings):
    discount_stacking: Literal["additive", "multiplicative"] = "additive"  # Q-B1
    rounding: Literal["to_dong_last_line", "per_line"] = "to_dong_last_line"  # Q-B2
    promotion_applies_at: Literal["order_open", "order_close"] = "order_open"  # Q-B3


class ReturnRules(BaseSettings):
    allow_cross_store: bool = False  # Q-B4
    demote_tier_on_return: bool = False  # Q-B5
    allow_negative_balance: bool = True  # Q-B6
```

> ⚠️ **Hai cái có hệ quả cấu trúc**, cần chốt *một* giá trị từ migration đầu:
> - **`rounding`** quyết định bất biến `subtotal = total + discount` có giữ được không. Đây là
>   `CHECK` constraint — làm tròn sai thì **giao dịch bị từ chối**, không phải chỉ lệch số.
> - **`allow_negative_balance`** quyết định có `CHECK (balance >= 0)` trên `point_balance`
>   hay không. Thêm/bỏ sau là migration.

---

## 6. Bất biến & cách cưỡng chế

| # | Bất biến | Cưỡng chế bằng |
|---|---|---|
| INV-1 | `sale.subtotal = total + discount_tier + discount_promo` | `CHECK` constraint |
| INV-2 | `SUM(sale_payment.amount) = sale.total` | **Trigger** (liên dòng, `CHECK` không làm được) + kiểm tra DI-3 |
| INV-3 | `SUM(sale_line.line_total) = sale.subtotal` | Trigger + DI-2 |
| INV-4 | `point_balance.balance = SUM(point_ledger.delta)` | **Job đối soát tăng dần** `central.ops.reconcile` (AT-10, DI-1). Lệch lưu ở `reconciliation_drift` tới khi được sửa; `--full` hằng tháng |
| INV-5 | Số lượng trả ≤ số lượng đã mua của dòng gốc | Kiểm tra ở use case + job rà soát |
| INV-6 | Mọi `sale` ở cửa hàng có mặt ở trung tâm sau ≤ 1 giờ | Job kiểm tra (DI-5) |

**Nguyên tắc:** bất biến trong một dòng → `CHECK`. Liên dòng → trigger. Liên hệ thống → job
đối soát. **Không bao giờ chỉ dựa vào tầng ứng dụng.**

---

## 7. Warehouse (ClickHouse — star schema)

### Dimension

```
dim_date(date_key, full_date, day, month, quarter, year, week,
         weekday_name, is_weekend, is_holiday)
dim_store(store_key, store_id, name, city, region, opened_at, is_current)
dim_employee(employee_key, employee_id, name, role, store_key, is_current)   -- không có salary
dim_product(product_key, product_id, sku, name, category, subcategory,
            price, valid_from, valid_to, is_current)            -- SCD2 theo giá
dim_customer(customer_key, customer_id, phone_masked, name_masked,
             tier, joined_at, valid_from, valid_to, is_current) -- SCD2 theo hạng
dim_promotion(promotion_key, promotion_id, name, type, valid_from, valid_to)
dim_shift(shift_key, shift_id, store_key, business_date,                     -- 🆕
          opened_by_employee_key, opened_at, closed_at, variance)
```

### Fact

```sql
-- Mức chi tiết nhất: MỘT DÒNG SẢN PHẨM
fact_sale_line(
    date_key, business_date,          -- 🆕 ngày kinh doanh ≠ ngày lịch
    store_key, shift_key,             -- 🆕
    employee_key, customer_key, product_key, promotion_key,
    sale_id, line_no,
    quantity, unit_price, line_total, discount_allocated,
    net_amount,                       -- line_total − discount_allocated; Σ theo đơn = total
    is_return UInt8,                  -- 🆕 phân biệt bán / trả
    sale_status, occurred_at, _recorded_at
) ENGINE = MergeTree                  -- thay trọn phân vùng (insert_overwrite), xem dưới
  PARTITION BY toYYYYMM(occurred_at)
  ORDER BY (store_key, date_key, sale_id, line_no);

-- 🆕 BẢNG RIÊNG cho thanh toán — KHÔNG gộp vào fact_sale_line
fact_payment(
    date_key, business_date, store_key, shift_key,
    sale_id, seq, method, amount, sale_status, occurred_at, _recorded_at
) ENGINE = MergeTree
  PARTITION BY toYYYYMM(occurred_at)
  ORDER BY (store_key, date_key, sale_id, seq);

-- Ánh xạ gần 1-1 từ point_ledger
fact_point_event(
    date_key, store_key, customer_key,
    event_id, sale_id, delta, reason, occurred_at, _recorded_at
) ENGINE = MergeTree
  PARTITION BY toYYYYMM(occurred_at)
  ORDER BY (customer_key, date_key, event_id);
```

> ⚠️ **Vì sao `fact_payment` phải tách riêng:** một đơn có N dòng sản phẩm **và** M hình thức
> thanh toán. Nhét chung một bảng thì join sẽ tạo tích Descartes → **nhân doanh thu lên N×M
> lần**. Đây là lỗi mô hình chiều kinh điển, và nó chỉ xuất hiện *sau khi* thêm `sale_payment`.

### Khác biệt so với v1

| v1 | v2 |
|---|---|
| Fact mức đơn hàng, pivot wide `product_id_1..N` | Fact **mức dòng sản phẩm**, giữ dạng long |
| `product_key String` vs `dim_product.product_key UInt32` | Kiểu surrogate key thống nhất + test `relationships` |
| `MergeTree` thuần → chạy lại là nhân đôi | Bronze: `insert_deduplication_token`. Mart: thay trọn phân vùng tháng bị ảnh hưởng (`insert_overwrite`). **Không** `ReplacingMergeTree` ở mart: nó che nhân đôi khỏi bộ đối soát L4 |
| Dimension không bao giờ được nạp | Mỗi dim là một dbt model có test |
| Không có nguồn cho fact điểm | `fact_point_event` ← `point_ledger` |
| Không có khái niệm ca làm việc | `dim_shift` + `shift_key` |
| Một `payment_method` đơn lẻ | `fact_payment` riêng |

**Ghi chú:** `point_ledger` gần như *chính là* `fact_point_event`. Chọn mô hình ledger ở
[ADR-002](adr/002-point-ledger.md) làm phần phân tích loyalty gần như miễn phí.

---

## 8. Xử lý PII

| Trường | Cửa hàng | Trung tâm | Warehouse |
|---|---|---|---|
| Số điện thoại | Hash (tra cứu) + mã hóa (hiển thị) | Như cửa hàng | **Mask**: `0901***567` |
| Tên khách | Mã hóa | Mã hóa | **Mask**: `Nguyễn V. A.` |
| `customer_id` | UUID | UUID | UUID (dùng để join) |

Warehouse **không bao giờ** chứa PII đọc được. Phân tích theo `customer_id` là đủ. Mọi lần đọc
PII ở hệ vận hành đều ghi nhật ký (NFR-06). **Không đưa PII vào span attribute hay log**
([doc 10](10-observability.md)).

---

## 9. Đã đủ 9 dataset theo spec gốc

| Dataset (spec gốc) | Bảng |
|---|---|
| Cửa hàng | `store` |
| Khu vực | `region` *(có phân cấp)* |
| Sản phẩm | `product`, `category`, `product_price_history` |
| Khách hàng | `customer`, `point_balance`, `point_ledger` |
| Nhân viên | `employee` |
| Giao dịch | `sale`, `sale_line`, `shift` |
| Kho hàng | `warehouse`, `stock` |
| Thanh toán | `sale_payment` |
| Khuyến mãi | `promotion` |

---
*Changelog: 2026-09-24 (lần 2) — §4.3: partition `point_ledger` cho cả tháng trước (migration
`0006`). §7: `dim_shift` có dòng inferred cho ca đang mở (ca chỉ lên trung tâm khi đóng; DAG giữa
ngày làm test relationships đỏ — phát hiện trên compose với bộ giả lập `virtual`).*

*Changelog: 2026-09-24 — §7 theo hiện thực dbt (`data_platform/dbt/`): fact dùng `MergeTree`
+ `insert_overwrite` thay vì `ReplacingMergeTree`. Phân vùng được thay trọn nên không cần
lưới an toàn, và lưới đó sẽ che lỗi nhân đôi mà audit L4 phải thấy. Thêm `net_amount`,
`sale_status`, `_recorded_at` (mốc tăng dần của bẫy 5), `sale_id` trong `fact_point_event`
(để đối soát theo đơn). Khóa thay thế = `cityHash64` của khóa nghiệp vụ (tất định: dựng lại ra
đúng khóa cũ), 0 = "không có". Dim có **inferred member** (`is_inferred = 1`) cho khóa thấy
trong giao dịch mà master data chưa có: trung tâm nhận đơn không cần khóa ngoại, nên mart phải
chịu được. Chưa có ở giai đoạn A: `dim_promotion`/`promotion_key` (khuyến mãi là C),
`dim_date.is_holiday` (cần lịch lễ chính thức), SCD2, `phone_masked`/`name_masked` (không PII).*

*Changelog: 2026-09-17 — sửa hai chi tiết phát hiện khi viết migration thật và chạy trên
PostgreSQL 18.6: (a) `CHECK (total >= 0)` trên `sale` nới thành
`status = 'RETURN' OR total >= 0` vì đơn trả hàng có số âm ở cả ba bảng; (b) `point_ledger`
đổi sang `PRIMARY KEY (event_id, occurred_at)` — Postgres bắt buộc PK chứa cột phân vùng.
Cả hai đã được kiểm chứng bằng test ở `tests/integration/`.*

*Changelog: 2026-09-23 (lần 2) — migration `0004_central_extract_watermarks`: `customer.recorded_at`,
index `point_ledger(recorded_at)`, trigger watermark trên 4 bảng, hàm `extract_horizon()`
([17 §4](17-data-flow.md) bẫy 1–3).*

*Changelog: 2026-09-11 — viết lại toàn bộ: gộp 8 lỗ hổng schema từ [business/](business/),
thêm `dim_shift` + `fact_payment` cho warehouse, tách quy tắc nghiệp vụ thành dữ liệu vs cấu
hình, bổ sung bảng bất biến và index cho báo cáo vận hành.*
