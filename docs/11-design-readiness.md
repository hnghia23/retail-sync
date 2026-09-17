# 11 — Sẵn sàng cho giai đoạn thiết kế

> **Trạng thái:** ✅ Giai đoạn **phân tích & quyết định** đã hoàn tất (2026-09-11).
> ✅ Phần lớn giai đoạn **thiết kế chi tiết** cũng đã xong (2026-09-12) — xem §6.
> Còn lại là sản phẩm của việc viết code, không phải tài liệu thiết kế.

---

## 1. Những gì đã chốt — không mở lại trừ khi có bằng chứng mới

### Kiến trúc

| | Quyết định |
|---|---|
| Mô hình | Edge tự chủ (offline-first) + trung tâm hợp nhất + nền tảng dữ liệu T+1 |
| Ranh giới chịu lỗi | Cửa hàng ↔ Trung tâm — **đứt bất cứ lúc nào, không được chặn bán hàng** |
| Điểm tích lũy | **Ledger append-only**, số dư là kết quả suy ra |
| Đồng bộ | **Transactional Outbox + HTTP** (→ Redpanda + CDC ở v2) |
| Cấu trúc edge | **Modular monolith** 3 module: `pos` · `loyalty` · `reporting` |
| Ba lớp truy vấn | A vận hành (T+0, PG cửa hàng) · B quản lý (T+0/T+1, PG trung tâm) · C phân tích (T+1, warehouse) |

### Stack

```
Edge         PostgreSQL 18 · Redis 7 · FastAPI · HTMX + Alpine.js
Trung tâm    PostgreSQL 18 · FastAPI
Đồng bộ      Outbox + HTTP
Lake         MinIO + Parquet (phân vùng theo NGÀY NẠP)
Warehouse    ClickHouse
Transform    dbt-core + astronomer-cosmos
Điều phối    Airflow 3 (LocalExecutor)
Giám sát     OpenTelemetry + grafana/otel-lgtm · pg_stat_statements · system.query_log
BI           Metabase (Could)
Nền          Python 3.12 · uv · SQLAlchemy 2 async + Alembic · pytest + testcontainers
             ruff · mypy · import-linter · GitHub Actions
```

### Ưu tiên

> **Ổn định + scale > đầy đủ tính năng.** Verification là Must; tính năng phụ là Could.

---

## 2. Mười ràng buộc thiết kế bắt buộc

Đây là danh sách dễ quên nhất, và mỗi cái đều đã có lý do cụ thể. **Đọc lại khi bắt đầu mỗi
module.**

| # | Ràng buộc | Nguồn |
|---|---|---|
| 1 | **`synchronous_commit = on`** ở Postgres cửa hàng — không được tắt để tối ưu | Cửa hàng không có UPS ([B02 I01](business/02-edge-cases.md)) |
| 2 | **Tự động tạo partition** `point_ledger` trước 3 tháng + kiểm tra hằng ngày | Rủi ro sập cao nhất ([08 §4.1](08-reliability-and-scale.md)) |
| 3 | **Bronze phân vùng theo `recorded_at`** (ngày nạp), không theo `occurred_at` | Dữ liệu đến trễ không phải viết lại lịch sử ([ADR-008 C3](adr/008-remaining-decisions.md)) |
| 4 | **`insert_deduplication_token`** là cơ chế idempotency chính của ClickHouse | `ReplacingMergeTree` chỉ eventual ([ADR-005](adr/005-clickhouse-warehouse.md)) |
| 5 | **`fact_payment` tách riêng** khỏi `fact_sale_line` | Gộp chung → nhân doanh thu N×M lần ([05 §7](05-data-model.md)) |
| 6 | **Xếp hạng dùng `lifetime_earned`**, không phải `balance` | Tiêu điểm không được làm tụt hạng ([ADR-002](adr/002-point-ledger.md)) |
| 7 | **Trace context (`traceparent`) lưu trong payload outbox**, dùng span link | Nối giao dịch gốc với lần đồng bộ hàng giờ sau ([10 §4](10-observability.md)) |
| 8 | **Job đối soát phải tăng dần** ngay từ bản đầu | Quét toàn bộ không khả thi ở T3 ([08 §4.2](08-reliability-and-scale.md)) |
| 9 | **Autovacuum riêng cho `outbox`** + partial index | Bảng duy nhất sinh bloat ([05 §3.5](05-data-model.md)) |
| 10 | **Không PII trong log, span, warehouse** — chỉ `customer_id` | NFR-06 |

## 3. Ba bất biến phải cưỡng chế bằng máy

Không dựa vào kỷ luật con người:

| | Bất biến | Công cụ |
|---|---|---|
| Kế toán | `subtotal = total + discount_tier + discount_promo` | `CHECK` constraint |
| Thanh toán | `SUM(sale_payment.amount) = sale.total` | Trigger + kiểm tra DI-3 |
| Sổ cái | `point_balance.balance = SUM(point_ledger.delta)` | Job đối soát hằng ngày |
| *Ranh giới module* | `pos` ↮ `loyalty` không import chéo tầng trong | `import-linter` trong CI |

---

## 4. Giai đoạn thiết kế cần sản xuất ra gì

Phân tích xong rồi; giờ là thiết kế. Bốn nhóm sản phẩm:

### 4.1. Hợp đồng API

- [ ] OpenAPI cho **Edge API**: `/auth`, `/products`, `/sales`, `/returns`, `/shifts`, `/customers`, `/reports`
- [ ] OpenAPI cho **Central API**: `/events` (ingest), `/customers/{id}`, `/master-data`, `/reports`
- [ ] **Schema sự kiện** cho outbox: `SaleCompleted`, `PointsEarned`, `PointsReturned`, `CustomerCreated`, `ShiftClosed` — có version từ đầu

### 4.2. Thiết kế module

- [ ] Ranh giới `pos` ↔ `loyalty` ↔ `reporting`: interface `LoyaltyPort`, `ReportingPort`
- [ ] Hợp đồng `import-linter` (viết trước khi viết code)
- [ ] Domain model thuần: `Sale`, `SaleLine`, `Payment`, `PointEvent`, `Shift`
- [ ] Use case: `PlaceSale`, `ReturnItems`, `OpenShift`, `CloseShift`, `ResolveCustomer`

### 4.3. Thiết kế dữ liệu

- [ ] DDL Alembic hoàn chỉnh từ [05-data-model.md](05-data-model.md)
- [ ] Chiến lược `pg_partman` cho `point_ledger`
- [ ] Cây thư mục dbt: `staging/` → `intermediate/` → `marts/`
- [ ] Layout lake: đường dẫn phân vùng cụ thể

### 4.4. Thiết kế vận hành

- [ ] `compose.yaml` với profile: `edge` · `central` · `data` · `bi` · `observability`
- [ ] Sơ đồ tuần tự cho 5 luồng: bán hàng · trả hàng · đồng bộ lên · kéo master data · chốt ca
- [ ] Sơ đồ máy trạng thái: vòng đời `sale` (COMPLETED → VOIDED/RETURN), vòng đời `shift`
- [ ] Danh sách span OTel + thuộc tính cho từng đường đi quan trọng

---

## 5. Thứ tự đề xuất cho giai đoạn thiết kế

```
1. Schema sự kiện + hợp đồng API      ← ranh giới trước, chi tiết sau
2. Domain model + use case             ← lõi nghiệp vụ, không phụ thuộc hạ tầng
3. DDL Alembic                         ← dẫn xuất từ domain, không ngược lại
4. compose.yaml + profile              ← đủ để chạy spike ngày 0
5. Sơ đồ tuần tự 5 luồng               ← phát hiện lỗ hổng trước khi code
```

**Nguyên tắc:** thiết kế **ranh giới** trước, **chi tiết** sau. Ranh giới sai thì sửa rất đắt;
chi tiết sai thì sửa rẻ.

---

## 6. Cập nhật — đã bổ sung (2026-09-12)

Sau khi hoàn tất §4, các mục sau đã được viết:

| Mục ở §4 | Đã có tại |
|---|---|
| Schema sự kiện outbox | [12-event-schema.md](12-event-schema.md) |
| OpenAPI Edge API + Central API | [13-api-contracts.md](13-api-contracts.md) |
| Sơ đồ tuần tự 4 luồng còn lại | [14-sequence-flows.md](14-sequence-flows.md) |
| `compose.yaml` + `.env.example` | [infra/compose.yaml](../infra/compose.yaml), [infra/.env.example](../infra/.env.example) |
| Glossary | [15-glossary.md](15-glossary.md) |
| Bản đồ AT/CH/LD/DI → test | [16-test-plan.md](16-test-plan.md) |
| README gốc mô tả v1 | Đã viết lại, trỏ về `docs/` |

**Còn lại từ §4 — trạng thái cập nhật 2026-09-17:**

| Việc | Trạng thái |
|---|---|
| DDL Alembic thật | ✅ Xong — `packages/{edge,central}/migrations/`, đã chạy trên PG 18.6, có test bất biến |
| Hàm tự tạo partition `point_ledger` | ✅ Xong — `ensure_point_ledger_partitions()` + view sức khỏe + `central.ops.partitions` |
| Interface `LoyaltyPort` + `import-linter` | ✅ Xong — `edge/pos/application/ports.py`, `.importlinter` (7 hợp đồng) |
| Dockerfile từng service | ✅ Xong — `infra/docker/*.Dockerfile` |
| Domain thuần (tính tiền, tính điểm, xếp hạng) | ✅ Xong — coverage 90–98%, chạy không cần Docker |
| Use case `PlaceSale` | ✅ Xong — test bằng fake (unit) và Postgres thật (integration) |
| Adapter Postgres + `POST /sales` + `GET /products` | ✅ Xong — một transaction, đã kiểm 5 bảng cùng commit |
| `LoyaltyService` (hiện thực `LoyaltyPort`) | ✅ Xong — tích/hoàn điểm, ledger + outbox cùng `event_id` |
| Xác thực JWT + Argon2 | ✅ Xong — `POST /auth/login`, `require_role()` bảo vệ mọi route khác (docs/13 §1) |
| Script nạp `crawl_data/` thành seed | ✅ Xong — `central.ops.seed` (region/store/product, 3515 cửa hàng thật, 5556 sản phẩm thật), `edge.ops.seed` (product_cache một cửa hàng) |
| UI POS (`/ui/login`, `/ui/pos`, quét hàng, thanh toán) | ✅ Xong — HTMX + Alpine vendor cục bộ, gọi đúng use case production |
| OpenTelemetry auto-instrumentation | ✅ Xong — FastAPI + asyncpg + Redis, idempotent qua nhiều lần `create_app()`. Xác minh thật: trace `edge-api`/`central-api` thấy được qua Tempo (docs/06 spike S5) |
| `pg_stat_statements` | ✅ Xong — bật cho mọi Postgres qua `shared_preload_libraries`, `track=all` để thấy cả câu lệnh trong PL/pgSQL. Xác minh thật: bắt được `ensure_point_ledger_partitions()` 23.68ms |
| **→ Tuần 1 đã đóng cổng (2026-09-18)** — xem docs/06-roadmap.md, tổng kết đầy đủ ở [docs/progress/2026-09-18-tong-ket-tuan-1.md](progress/2026-09-18-tong-ket-tuan-1.md) | |
| Tra khách qua Redis + trung tâm (bước 1 và 3 của docs/13 §4) | ⏳ Chưa — tuần 2 ngày 9 |
| `POST /returns`, `/shifts`, `/reports` | ⏳ Chưa — tuần 2 ngày 10 (`POST /shifts/open` hiện có bản UI tạm ở `/ui/shifts/open`, sẽ bị thay) |
| Sync worker đẩy outbox | ⏳ Chưa — tuần 2 ngày 8 |

---

## 7. Những gì vẫn chưa biết — và ổn khi chưa biết

Thành thật về giới hạn của bộ docs này:

| Chưa biết | Khi nào biết | Có chặn thiết kế không? |
|---|---|---|
| RAM thật của cả stack | Spike ngày 0 | ❌ Không — chỉ đổi số cửa hàng mô phỏng |
| Airflow 3 + cosmos + ClickHouse có phối hợp trơn không | Spike ngày 0 | ❌ Không — có phương án `BashOperator` |
| `uuidv7()` qua SQLAlchemy 2 async có ổn không | Spike ngày 0 | ❌ Không — có phương án sinh ở tầng Python |
| Overhead của OpenTelemetry | Tuần 4 | ❌ Không |
| Ngưỡng Postgres thật (12 Tr dòng?) | Tuần 4 | ❌ Không — chỉ ảnh hưởng thời điểm cần ClickHouse |
| 4 tuần có đủ không | Hết tuần 2 | ⚠️ **Có** — cổng tuần 2 là điểm quyết định cắt phạm vi |

**Không có ẩn số nào chặn việc bắt đầu thiết kế.** Mọi thứ chưa biết đều chỉ ảnh hưởng *cách
triển khai*, không ảnh hưởng *cấu trúc*.

---

## 8. Bản đồ docs

```
00-context           Bối cảnh, ràng buộc, phạm vi
01-requirements      FR + NFR + 10 kịch bản nghiệm thu
02-scale-capacity    Số liệu quy mô — CHẶN việc chọn công nghệ theo cảm tính
03-architecture      Kiến trúc, 3 ranh giới, 3 lớp truy vấn, mô hình chịu lỗi
04-tech-stack        Bảng stack + scale seam
05-data-model        ⭐ Schema đầy đủ — nguồn sự thật cho DDL
06-roadmap           Ngày 0 spike + 4 tuần + MoSCoW
07-stack-decision    ⭐ Lý do từng lựa chọn + độ tự tin + chi phí đảo ngược
08-reliability       ⭐ FMEA, backpressure, observability, kế hoạch chứng minh
09-v1-postmortem     v1 sai ở đâu — 5 bài học
10-observability     Săn bottleneck: OTel, trace qua outbox, profiling DB
11-design-readiness  ⭐ Doc này — điểm vào giai đoạn thiết kế
12-event-schema      Hợp đồng sự kiện outbox
13-api-contracts     Route Edge API + Central API
14-sequence-flows    4 luồng còn thiếu: trả hàng, master data, chốt ca, đồng bộ lỗi
15-glossary          Thuật ngữ dùng thống nhất
16-test-plan         Bản đồ AT/CH/LD/DI → file test
99-original-spec     Spec gốc nguyên văn

adr/001..009         Vì sao — kèm phương án đã loại và điều kiện xem lại
business/B01..B04    Vận hành thật, ~50 tình huống, workload phân tích, hệ quả stack

infra/compose.yaml   Compose scaffold theo profile (edge/central/data/bi/observability)
infra/.env.example   Mẫu biến môi trường
```

**Nếu chỉ đọc được ba doc:** [07-stack-decision](07-stack-decision.md) *(quyết định gì và vì
sao)* · [05-data-model](05-data-model.md) *(dữ liệu trông ra sao)* · doc này *(làm gì tiếp)*.

---
*Changelog: 2026-09-11 — tạo mới, chốt giai đoạn phân tích.*
