# 09 — Postmortem prototype v1

Doc này ghi lại **vì sao làm lại từ đầu**, và quan trọng hơn: **những lỗi nào phải không
được lặp lại**. Mỗi lỗi ở đây tương ứng với một quyết định thiết kế trong v2.

Đây không phải để phê phán — v1 đã dựng thành công phần khó nhất (chọn stack, dựng
compose, dựng star schema) và chính nó sinh ra hiểu biết để thiết kế v2.

---

## 1. Nguyên nhân gốc rễ

Không phải lỗi kỹ thuật nào cụ thể. Ba nguyên nhân hệ thống:

### 1.1. Chọn công nghệ trước khi tính quy mô

Cassandra và Kafka được đưa vào ngay từ đầu vì "để scale", nhưng chưa từng có một con số
nào được tính. Khi tính ra ([02](02-scale-capacity.md)), tải đỉnh ở 2000 cửa hàng chỉ là
**67 writes/giây** — một PostgreSQL dư 25–100 lần.

Kết quả: 2 trong 8 hệ thống hạ tầng không phục vụ mục đích gì, nhưng vẫn tiêu thụ RAM,
thời gian dựng, thời gian gỡ lỗi và **hạn mức chú ý**.

→ v2: [02-scale-capacity.md](02-scale-capacity.md) viết **trước** [04-tech-stack.md](04-tech-stack.md).
Quy tắc: không dùng công nghệ phân tán khi một node còn dư trên 10 lần.

### 1.2. Dựng hạ tầng theo chiều rộng thay vì chiều sâu

v1 dựng **tất cả** thành phần đến mức "khởi động được", nhưng không luồng nào thông từ đầu
đến cuối. Bằng chứng rõ nhất:

- `pos_service/adapters/loyalty_api.py` — **0 byte**
- `loyalty_service/kafka/producer.py` — **0 byte**
- `loyalty_service/kafka/consumer.py` — **0 byte**
- `central/scripts/load_to_minio.py` — **0 byte**
- `loyalty_service/services/loyalty_service.py` — chỉ có `pass`

Bốn file rỗng nằm đúng ở bốn điểm nối quan trọng nhất. Hạ tầng có mặt, hệ thống thì không.

Hệ quả: **tính năng #2 (tích điểm) — một trong hai yêu cầu chính — chưa bao giờ hoạt động.**

→ v2: [06-roadmap.md](06-roadmap.md) có cổng kiểm tra mỗi tuần, mỗi cổng đòi một luồng
**end-to-end** chạy được, không đòi thành phần "khởi động được".

### 1.3. Không ghi lại quyết định

Không có chỗ nào giải thích vì sao chọn Cassandra, vì sao tách POS/Loyalty thành hai app,
vì sao Kafka. Nên không ai (kể cả tác giả sau vài tuần) rà soát lại được, và các lựa chọn
sai cứ thế tồn tại.

→ v2: bộ [ADR](adr/) ghi bối cảnh, phương án đã loại, scale seam, và **điều kiện xem lại**
cho mỗi quyết định lớn.

---

## 2. Lỗi thiết kế → sửa trong v2

| # | Lỗi v1 | Hậu quả | Sửa trong v2 |
|---|---|---|---|
| D1 | **Điểm lưu dạng số dư cập nhật được**, ở cả Postgres cửa hàng lẫn Cassandra trung tâm | Race condition mất điểm khách; hai nguồn sự thật không hòa giải được; không kiểm toán được | Ledger append-only, số dư là kết quả suy ra — [ADR-002](adr/002-point-ledger.md) |
| D2 | **Thiếu `store_id`** trong bảng `transactions` | POS không biết mình là cửa hàng nào → DAG buộc hardcode `store_key = 1` → mọi phân tích theo cửa hàng sụp đổ | `store_id` bắt buộc trong mọi bảng giao dịch — [05](05-data-model.md) §1 |
| D3 | `transaction_id` là `INT AUTO_INCREMENT` | Cửa hàng 1 và 2 đều sinh `1,2,3...` → **đụng ID khi gộp về trung tâm** | UUIDv7 sinh tại cửa hàng |
| D4 | **Publish Kafka sau `commit()`** ngoài transaction | Dual-write: app chết giữa hai bước → event mất vĩnh viễn | Transactional outbox — [ADR-003](adr/003-outbox-not-kafka.md) |
| D5 | **Không có đường trung tâm → cửa hàng** | Luồng quan trọng nhất của spec ("khách lần đầu → gọi trung tâm") chưa từng tồn tại | Nằm trong cổng tuần 2, kịch bản AT-05 |
| D6 | Điểm (`earned_point`) do **client tự khai** | Client gửi bao nhiêu điểm cũng được | Server tính từ `total`, quy tắc lấy từ `earn_rule` |
| D7 | Không có idempotency ở bất kỳ đâu | Gửi lại = cộng điểm hai lần | `event_id` là khóa chính, `ON CONFLICT DO NOTHING` |
| D8 | Xếp hạng theo số dư (dự kiến) | Khách tiêu điểm sẽ **bị tụt hạng** | Xếp hạng theo `lifetime_earned` — [05](05-data-model.md) §4 |
| D9 | POS gọi Loyalty đồng bộ (dự kiến), không có fallback | Loyalty lỗi → không bán được hàng | Timeout 500ms, bỏ qua, suy giảm có kiểm soát |
| D10 | Không có xác thực | Spec yêu cầu "nhân viên đăng nhập" — chưa có | JWT + Argon2 + vai trò, ngay tuần 1 |
| D11 | Hai app hai framework (FastAPI + Flask), không nối | Chi phí phối hợp tiêu hết ngân sách trước khi xong tính năng | Modular monolith, ranh giới cưỡng chế bằng `import-linter` — [ADR-004](adr/004-modular-monolith.md) |
| D12 | `salary` nằm trong DB của POS | Dữ liệu HR nhạy cảm ở sai hệ thống | Loại khỏi mô hình — [00](00-context.md) §Phạm vi |

## 3. Lỗi pipeline dữ liệu → sửa trong v2

| # | Lỗi v1 | Hậu quả | Sửa trong v2 |
|---|---|---|---|
| P1 | `WHERE created_at <= NOW() - INTERVAL 7 DAY` chạy `@weekly` | **Lấy lại toàn bộ lịch sử mỗi tuần** → nhân bản dữ liệu | Partition có tham số thời gian, ghi đè theo phân vùng |
| P2 | **Không idempotent** — chạy lại là nhân đôi | Warehouse không đáng tin | `ReplacingMergeTree` + dbt incremental + AT-07 |
| P3 | **5 bảng dimension không bao giờ được nạp** | Star schema rỗng ruột, không join được gì | dbt model cho mọi dim + test `relationships` |
| P4 | `fact_sales.product_key String` vs `dim_product.product_key UInt32` | Không join được | Kiểu surrogate key thống nhất, test cưỡng chế |
| P5 | Nhét `transaction_id` (Int) vào `sales_key UUID`, `customer_id` (String) vào `customer_key UInt32` | Type error khi insert | Tra cứu surrogate key rõ ràng ở tầng intermediate |
| P6 | **Pivot wide** (`product_id_1..N`) rồi melt ngược ở DAG sau | Vòng vo vô ích, **mất item khi đơn dài hơn số cột** | Giữ dạng long xuyên suốt, fact ở mức dòng sản phẩm |
| P7 | **Không có nguồn cho `fact_point_transaction`** | Spec "Loyalty → Lake → Warehouse" chưa tồn tại | `fact_point_event` ← `point_ledger` |
| P8 | Hai store dùng **chung** `mysql_conn_id` | Cả hai extract cùng một database | Mỗi cửa hàng một connection riêng |
| P9 | `open('config/pos_config.json')` đường dẫn tương đối ở top-level DAG | Phụ thuộc cwd của scheduler | Config inject qua resource |
| P10 | Không có kiểm tra chất lượng dữ liệu | Lỗi lặng lẽ | dbt test là bắt buộc trong pipeline |

## 4. Lỗi chặn hệ thống chạy

Những lỗi khiến v1 không khởi động hoặc không xử lý được request nào:

| # | Vị trí | Lỗi |
|---|---|---|
| B1 | `central/init/cassandra/schema.cql:20` | `CREATE TABLE IF NOT EXIST` — thiếu chữ `S`, thiếu `;` cuối → `cassandra-init` fail, bảng `customers_info` **không bao giờ được tạo** |
| B2 | `pos_service/adapters/mysql_repo.py:26` | INSERT cột `unit_price` vào `transaction_item`, nhưng schema **không có cột này** → mọi đơn hàng đều fail |
| B3 | `pos_service/api/order_router.py:5`, `api/product.py:4` | Import `from domain.order` (thiếu prefix `pos_service.`) trong khi `main.py` import `pos_service.api...` → ImportError khi chạy từ root |
| B4 | `pos_service/api/product.py:7` | `product_ids: List[int]` nhưng product id là VARCHAR (`'MNLS-ho'`) → API tra giá luôn trả rỗng |
| B5 | `loyalty_service/models/*.py` | Thiếu `__table_args__ = {"schema": "loyalty"}` → ORM tìm `public.customer_info`, không thấy bảng |
| B6 | `loyalty_service/config.py:6` | Default `postgres/loyalty_db` nhưng compose tạo DB `loyalty` user `admin`, port `5433` → không kết nối được. `.env` rỗng |
| B7 | `loyalty_service/models/loyalty_tier.py` | `tier_name/min_points/discount_rate` vs SQL `tier/min_point/discount_percentage` → sai toàn bộ tên cột |
| B8 | `store/init/postgres/loyalty_schema.sql` | Bảng `loyalty_tier` **không có seed data** → `tier` luôn NULL → `discount_percentage` vô dụng |
| B9 | `store/main.py:24` | MySQL connection ở module scope, `host="localhost"` hardcode, không pool → connection timeout sau vài phút idle là POS chết |
| B10 | — | `pos_service` và `loyalty_service` **không có Dockerfile, không có trong compose** |

**Bài học chung:** phần lớn lỗi này là **schema và code trôi khỏi nhau**. v1 sửa schema ở
một chỗ, code ở chỗ khác, không có gì bắt buộc hai bên khớp.

→ v2: migration có version (Alembic), model sinh từ schema, test tích hợp dùng DB thật qua
testcontainers. Lệch là CI đỏ ngay.

## 5. Vấn đề vận hành

| # | Vấn đề | Sửa trong v2 |
|---|---|---|
| O1 | Mật khẩu hardcode trong compose và `lake_to_dw.py:87`; `.env` rỗng | pydantic-settings + `.env.example`, rà quét secret trong CI |
| O2 | Stack 8 hệ thống, ~10 GB RAM, khởi động rất chậm | 5 hệ thống, ~5 GB, compose profile để chạy từng phần |
| O3 | Bốn phương ngữ SQL phải nắm (MySQL, Postgres, CQL, ClickHouse) | Hai (Postgres, ClickHouse) |
| O4 | Không có test nào | pytest + testcontainers + dbt test + 10 kịch bản nghiệm thu |
| O5 | Không có CI | GitHub Actions: lint → type → test → build |
| O6 | Không có log có cấu trúc, không healthcheck | structlog JSON + `trace_id`, healthcheck mọi service |

*(Điểm làm đúng: `central/data/` đã được gitignore — không file DB nào bị commit. Nhiều dự
án mắc lỗi này.)*

---

## 6. Cái gì giữ lại từ v1

Không phải mọi thứ đều bỏ. Ba thứ có giá trị thật:

| Giữ | Vì sao |
|---|---|
| `crawl_data/products/` (~400 KB sản phẩm thật, tiếng Việt) | Dữ liệu thật tốt hơn dữ liệu sinh giả. Dùng làm master data ngay tuần 1 |
| `crawl_data/store_info/` (cửa hàng theo tỉnh) | Nuôi `store` và `region` |
| Ý tưởng star schema ở `dw_schema.sql` | Cấu trúc dim/fact đúng hướng, chỉ sai chi tiết kiểu dữ liệu và thiếu loader |
| Cấu trúc clean architecture ở `pos_service` | `domain` / `use_cases` / `adapters` / `api` tách bạch tốt. v2 giữ nguyên tinh thần này |

Code v1 nên chuyển vào `legacy/` để tham khảo, **không xóa** — nó là tài liệu sống về những
gì đã thử.

---

## 7. Năm bài học mang sang v2

1. **Tính quy mô trước khi chọn công nghệ.** Một bảng tính 10 phút đã loại được hai hệ
   thống hạ tầng.
2. **Làm thông một luồng trước khi mở rộng chiều rộng.** Một luồng end-to-end chạy được
   giá trị hơn tám thành phần khởi động được.
3. **Ghi lại quyết định kèm phương án đã loại.** Lựa chọn không được ghi lại là lựa chọn
   không thể rà soát.
4. **File rỗng là dấu hiệu cảnh báo.** Bốn file 0 byte nằm đúng ở bốn điểm nối quan trọng
   nhất — đó là bản đồ chỉ ra hệ thống chưa tồn tại.
5. **Cưỡng chế bằng công cụ, đừng dựa vào kỷ luật.** Schema lệch code, ranh giới module bị
   rò rỉ, secret bị hardcode — cả ba đều bắt được bằng CI, không bắt được bằng ý chí.

---
*Changelog: 2026-09-11 — tạo mới dựa trên rà soát toàn bộ code v1.*
