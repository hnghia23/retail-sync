# 06 — Lộ trình 4 tuần

**Ngân sách:** 1 người, ~4 tuần, ~80–120 giờ làm việc thật, có Claude Code hỗ trợ.

Lộ trình này **cố tình cắt sâu**. Thà có một luồng end-to-end chạy thật còn hơn năm luồng
dở dang. Mỗi tuần có một **cổng kiểm tra** — không qua cổng thì không sang tuần sau, phải
cắt phạm vi thay vì cắt chất lượng.

---

> **Tiền đề:** stack đã chốt toàn bộ ([07-stack-decision.md](07-stack-decision.md)) nhưng
> **chưa kiểm chứng thực nghiệm**. Ngày 0 dành cho spike xác minh — xem dưới.

## Ngày 0 — Spike xác minh *(nửa ngày, làm trước tuần 1)*

Chốt trên giấy xong rồi, nhưng mọi con số vẫn là ước lượng. Nửa ngày này để phát hiện sai
lầm ở ngày 0 thay vì ngày 15.

| # | Kiểm chứng | Đạt khi | Trạng thái |
|---|---|---|---|
| S1 | `compose.yaml` rỗng: PG 18 + Airflow 3 LocalExecutor + ClickHouse + MinIO | Khởi động hết, **đo RAM thật** ≤ 8 GB | 🟡 Một phần — `edge`+`central`+`observability` đo được **~806 MiB** (2026-09-18); `data` (Airflow/ClickHouse/MinIO) chưa dựng |
| S2 | Migration Alembic dùng `uuidv7()` qua SQLAlchemy 2 async | Sinh được ID, driver không lỗi | ✅ Đạt — `uuidv7()` DEFAULT chạy thật trên PG 18.6, xác nhận sắp theo thời gian (`test_uuidv7_is_time_ordered`) |
| S3 | DAG rỗng chạy dbt qua `astronomer-cosmos` | Thấy từng dbt model là một task riêng, có lineage | ⏳ Chưa — tuần 3 |
| S4 | Nạp `crawl_data/products/` vào ClickHouse bằng hàm `s3()` từ MinIO | Đếm đúng số dòng | ⏳ Chưa — tuần 3 (đã nạp được vào Postgres qua `central.ops.seed`, xem tuần 1 ngày 4) |
| S5 | `grafana/otel-lgtm` lên, FastAPI rỗng gửi trace qua OTLP | Thấy trace trong Grafana/Tempo; **đo RAM thật** của container này | ✅ Đạt (2026-09-18) — trace `GET /health` của cả `edge-api` lẫn `central-api` thấy được qua Tempo API, kèm span con `asyncpg`/`redis` lồng đúng. RAM `otel-lgtm` lúc khởi động: **~414 MiB** |

**Nếu S1 trượt** (RAM vượt): giảm còn 2 cửa hàng mô phỏng, hoặc tách profile chặt hơn.
**Nếu S3 trượt:** dùng `BashOperator` chạy `dbt build` — mất lineage nhưng không chặn.
**Nếu S2 trượt:** sinh UUIDv7 ở tầng Python (`uuid6` package) thay vì trong DB.

Không cái nào trong bốn cái này đòi đổi quyết định stack — chỉ đổi cách triển khai.

## Nguyên tắc phân bổ thời gian

## Nguyên tắc phân bổ thời gian

Claude Code tăng tốc rất mạnh ở: sinh schema, CRUD, dbt model, docker config, test,
migration, docs. Tăng tốc rất ít ở: debug race condition, tinh chỉnh consistency, dựng và
gỡ lỗi hạ tầng, quyết định thiết kế.

→ Dồn thời gian người vào **tuần 2** (phần logic phân tán, khó nhất). Để Claude Code làm
nặng ở tuần 1 và tuần 3.

---

## Tuần 1 — Nền móng + POS chạy được

**Mục tiêu:** bán được một đơn hàng, lưu vào DB, có đăng nhập.

| Ngày | Việc | Ghi chú |
|---|---|---|
| 1 | Dựng repo: `uv`, ruff, mypy, pytest, cấu trúc thư mục, `import-linter` | UI dùng **HTMX + Alpine.js** — không cần Node/bundler |
| 1 | `compose.yaml` với profile: `edge`, `central`, `data`, `bi` | Postgres + Redis lên trước |
| 2 | Migration Alembic: schema cửa hàng ([05](05-data-model.md) §3) — **gồm `shift`, `sale_payment`, liên kết trả hàng một phần** | `CHECK` constraint + trigger INV-2/INV-3 ngay từ đầu. **Kèm `ALTER TABLE outbox SET (autovacuum_vacuum_scale_factor=0.02...)`** và 4 index báo cáo vận hành ([05 §3.6](05-data-model.md)) |
| 2 | Migration: schema trung tâm ([05](05-data-model.md) §4), `point_ledger` phân vùng | Phân vùng **từ ngày đầu**. Kèm `pg_partman` hoặc job tạo trước 3 tháng ([08 §4.1](08-reliability-and-scale.md)) |
| 3 | Domain thuần + test: tính tiền, tính điểm, xếp hạng | **Không phụ thuộc hạ tầng.** Đây là lõi, test kỹ. Quy tắc nghiệp vụ là **config object**, không hardcode ([05 §5](05-data-model.md)) |
| 3 | Xác thực: JWT + Argon2, vai trò, token mang `store_id` | FR-P01, FR-P02 |
| 4 | API POS: tra sản phẩm, tạo đơn (chưa có loyalty) | FR-P03, FR-P04, FR-P07 |
| 4 | Nạp dữ liệu thật từ `crawl_data/products/` vào master data | Dùng lại tài sản của v1 |
| 5 | UI POS tối thiểu: đăng nhập → quét hàng → thanh toán → hóa đơn | ✅ Xong (2026-09-18) — HTMX + Alpine vendor cục bộ (không CDN, nguyên tắc kiến trúc #1), `/ui/*` gọi ĐÚNG `place_sale()`/`login()` production |
| 5 | Test tích hợp với testcontainers | ✅ Xong — 34 test HTTP thật (`test_auth_http.py` + `test_web_ui.py`), 220 test toàn repo |
| 5 | **OpenTelemetry auto-instrumentation** + bật `pg_stat_statements` | ✅ Xong (2026-09-18) — xác minh thật: trace `edge-api`/`central-api` thấy trong Tempo; `pg_stat_statements` bắt được `ensure_point_ledger_partitions()` 23.68ms |

### 🚪 Cổng tuần 1
- [x] `docker compose --profile edge up` → API trả `200 /health` — xác minh thật 2026-09-18
- [x] Đăng nhập được, nhận JWT có `store_id`
- [x] Bán một đơn 3 sản phẩm, lưu đúng vào Postgres, bất biến `subtotal = total + discount` giữ đúng
- [x] Test domain đạt coverage > 80%, chạy không cần Docker (90–98%)

**Cổng tuần 1 ĐẠT (2026-09-18).** Không phải cắt UI hay dữ liệu crawl — cả hai đều chạy
thật, có test tự động, và một bug thật (`sale_payment.amount` dùng nhầm tiền khách đưa
thay vì tổng đơn) được chính test tự động bắt và sửa trước khi merge.

---

## Tuần 2 — Loyalty + Ledger + Đồng bộ + Báo cáo vận hành ⭐ *tuần khó nhất*

**Mục tiêu:** điểm đúng, kể cả khi mất mạng và khi gửi lặp.

Đây là tuần tạo ra giá trị thật của dự án. Nếu phải cắt ở đâu, **không cắt ở đây**.

| Ngày | Việc | Ghi chú |
|---|---|---|
| 6 | Module loyalty: `point_ledger` cục bộ, tính số dư, `lifetime_earned`, xếp hạng | [ADR-002](adr/002-point-ledger.md) |
| 6 | `LoyaltyPort` interface + cưỡng chế ranh giới bằng `import-linter` | [ADR-004](adr/004-modular-monolith.md) |
| 7 | Nối vào luồng bán hàng: **một transaction** cho sale + line + ledger + outbox | Điểm mấu chốt của cả hệ thống |
| 7 | Cache-aside Redis, **suy giảm được khi Redis chết** | AT-08 |
| 8 | API trung tâm: `POST /events` (dedupe `ON CONFLICT`), `GET /customers/{id}` | FR-C02, FR-C03 |
| 8 | Sync worker: `FOR UPDATE SKIP LOCKED`, lô, backoff + jitter, dead-letter | [ADR-003](adr/003-outbox-not-kafka.md) |
| 8 | **Truyền trace context qua outbox** — làm cùng lúc viết outbox | 🔴 Must — nối giao dịch gốc với lần đồng bộ hàng giờ sau ([10 §4](10-observability.md)) |
| 9 | Luồng ngược: khách lạ → gọi trung tâm, **timeout 500ms rồi bỏ qua** | FR-L06, không được chặn bán hàng |
| 9 | Số dư hiển thị = trung tâm + ledger cục bộ chưa gửi | Tránh số dư tụt lùi |
| 10 | Kéo master data có phiên bản xuống cửa hàng | FR-C01 |
| 10 | **Kịch bản AT-02, AT-03, AT-04, AT-06, AT-10** | Đây là bằng chứng hệ thống đúng |
| 10 | Job đối soát ledger vs snapshot — **viết dạng tăng dần ngay từ đầu** | NFR-05 bắt buộc. Quét toàn bộ không khả thi ở T3 ([08 §4.2](08-reliability-and-scale.md)) |
| 10 | 🆕 **Module `reporting`**: chốt ca, doanh thu hôm nay, tra đơn cũ | 🔴 Must — FR-R01…R03, đọc thẳng PG cửa hàng, chạy khi offline |

### 🚪 Cổng tuần 2 *(cổng quan trọng nhất)*
- [ ] 🆕 Chốt được một ca: mở ca → bán vài đơn → đóng ca → báo cáo ca đúng số, **kể cả khi ngắt mạng**
- [ ] 🆕 Bán một đơn thanh toán tách (tiền mặt + thẻ), `SUM(sale_payment) = sale.total`
- [ ] 🆕 Trả 2 trong 5 sản phẩm, điểm trừ đúng tỷ lệ
- [ ] **AT-02** — ngắt mạng trung tâm, bán 10 đơn, nối lại → 10 đơn lên đủ, điểm cộng đúng một lần
- [ ] **AT-03** — gửi lặp một sự kiện 5 lần → điểm chỉ cộng một lần
- [ ] **AT-04** — hai cửa hàng bán cho cùng khách đồng thời → số dư = tổng, không mất dòng
- [ ] **AT-06** — trả hàng → ledger có dòng âm, điểm trừ đúng
- [ ] **AT-10** — đối soát khớp tuyệt đối
- [ ] Ngắt Redis → vẫn bán được

**Nếu trượt:** dừng, không sang tuần 3. Cắt tuần 3 xuống tối thiểu (nạp thẳng ClickHouse,
bỏ lake) và dùng thời gian dư để đóng cổng này.

---

## Tuần 3 — Nền tảng dữ liệu

**Mục tiêu:** dữ liệu chảy vào warehouse, chạy lại không nhân đôi.

| Ngày | Việc | Ghi chú |
|---|---|---|
| 11 | MinIO + Airflow 3 lên compose (profile `data`) | Trích xuất bằng **Python thuần**, không `dlt` ([ADR-008 Q2](adr/008-remaining-decisions.md)) |
| 11 | Bronze phân vùng theo **`recorded_at` (ngày nạp)**, không phải `occurred_at` | ⚠️ **Ràng buộc quan trọng** — làm dữ liệu đến trễ không phải viết lại lịch sử ([ADR-008 C3](adr/008-remaining-decisions.md)) |
| 12 | Asset silver: làm sạch, khử trùng lặp, chuẩn hóa | |
| 12 | dbt-core + `dbt-clickhouse`, phân lớp staging/intermediate/marts | [ADR-005](adr/005-clickhouse-warehouse.md) |
| 13 | dbt: **toàn bộ 7 dimension** (gồm `dim_shift`) + test `relationships` | Lỗi nặng nhất của v1 — dim rỗng ruột |
| 13 | `dim_date` sinh bằng seed/macro | |
| 14 | dbt: `fact_sale_line` (mức dòng sản phẩm) | FR-C06 — idempotency chính bằng **`insert_deduplication_token`** + thay trọn phân vùng; `ReplacingMergeTree` chỉ là lưới an toàn ([ADR-005](adr/005-clickhouse-warehouse.md)) |
| 14 | dbt: **`fact_payment`** *(bảng riêng)* | ⚠️ Gộp vào `fact_sale_line` sẽ **nhân doanh thu N×M lần** ([05 §7](05-data-model.md)) |
| 14 | dbt: `fact_point_event` ← `point_ledger` | Gần như miễn phí nhờ [ADR-002](adr/002-point-ledger.md) |
| 15 | Test dữ liệu: `not_null`, `unique`, `relationships`, `accepted_values` | FR-C07 |
| 15 | **AT-07** — chạy pipeline 2 lần, số dòng không đổi | |
| 15 | Backfill theo khoảng ngày | FR-C09 |

### 🚪 Cổng tuần 3
- [ ] **AT-07** — chạy pipeline hai lần liên tiếp, `SELECT count()` không đổi
- [ ] Join được `fact_sale_line` với cả 7 dimension, không dòng nào mồ côi
- [ ] `SUM(fact_payment.amount)` theo ngày **khớp** `SUM(fact_sale_line.line_total)` — chứng minh không nhân đôi
- [ ] `dbt test` xanh toàn bộ
- [ ] Backfill 7 ngày quá khứ chạy được
- [ ] Xóa sạch warehouse rồi dựng lại hoàn toàn từ bronze

**Nếu trượt:** bỏ tầng silver (bronze → dbt trực tiếp), bỏ SCD2 (dùng SCD1), bỏ backfill.

---

## Tuần 4 — Chứng minh ổn định & scale ⭐ *tuần quan trọng thứ hai*

**Mục tiêu:** **chứng minh** hệ thống ổn định và scale được — không phải tuyên bố.

Theo ưu tiên của chủ dự án, tuần này ngang tầm quan trọng với tuần 2. Chi tiết từng thử
nghiệm ở [08-reliability-and-scale.md §6](08-reliability-and-scale.md).

| Ngày | Việc | Ghi chú |
|---|---|---|
| 16 | **Bộ sinh dữ liệu**: N cửa hàng × M đơn × D ngày, phân bố thực tế | Cao điểm trưa/tối, cuối tuần nhiều, Pareto cho sản phẩm |
| 16 | Nạp 1 năm dữ liệu T2, đo dung lượng thật | Ghi ngược vào [02](02-scale-capacity.md) |
| 17 | Metabase + 4 dashboard: doanh thu, sản phẩm, khách hàng, loyalty | FR-C08. Chốt Q4 |
| 17 | **Test tải LD-1…LD-4** bằng `k6`: 1 CH giờ cao điểm, 10 CH đồng bộ đồng thời, truy vấn lớp C trên dữ liệu T2 | 🔴 Must — [08 §6.3](08-reliability-and-scale.md) |
| 18 | **Test hỗn loạn CH-1…CH-7**: ngắt mạng, `kill -9` app, `kill -9` Postgres, đầy đĩa, tắt Redis, 10 CH phục hồi cùng lúc | 🔴 Must — [08 §6.1](08-reliability-and-scale.md) |
| 18 | Theo dõi tình trạng đồng bộ từng cửa hàng | FR-C10 — là observability |
| 19 | Siết bảo mật: rà secret, mask PII trong warehouse, nhật ký truy cập PII | NFR-06 |
| 19 | **Observability đầy đủ**: dashboard D1–D4, ngưỡng cảnh báo, `structlog` JSON | 🔴 Must — [10 §7](10-observability.md) |
| 19 | **Đo overhead của OTel**: chạy LD-1 hai lần, có và không instrumentation | 🔴 Must — nếu > 5% thì giảm tỷ lệ lấy mẫu ([ADR-009](adr/009-observability-stack.md)) |
| 19 | **Tự động tạo partition** (`pg_partman` hoặc job) + kiểm tra hằng ngày | 🔴 Must — rủi ro sập cao nhất, [08 §4.1](08-reliability-and-scale.md) |
| 20 | **Test ngâm 72h** chạy nền — theo dõi rò rỉ bộ nhớ, bloat `outbox`, trôi độ trễ | 🔴 Must — bắt lỗi suy thoái theo thời gian |
| 20 | **Kiểm chứng scale seam**: nạp 50 Tr dòng ledger, đo partition pruning; 200 kết nối đồng thời | 🔴 Must — [08 §4.4](08-reliability-and-scale.md) |
| 20 | CI GitHub Actions: lint → type → test → build | |
| 20 | Cập nhật toàn bộ docs với số liệu đo thật, README chạy thử | |

### 🚪 Cổng tuần 4 (nghiệm thu POC)

**Nghiệp vụ**
- [ ] Toàn bộ **AT-01 … AT-10** xanh, tự động
- [ ] 2–3 cửa hàng mô phỏng + trung tâm + data platform chạy đồng thời trên một máy
- [ ] `git clone` → `docker compose up` → chạy được theo README

**Ổn định** 🆕
- [ ] **CH-1…CH-7 đều đạt** — đặc biệt CH-3 (`kill -9` Postgres → 0 giao dịch đã commit bị mất)
- [ ] **Test ngâm 72h**: bộ nhớ không rò rỉ, `outbox` không bloat, p95 không trôi
- [ ] **DI-1…DI-5** đều xanh
- [ ] Mọi ngưỡng cảnh báo ở [08 §5](08-reliability-and-scale.md) đã cấu hình và **đã kích hoạt thử được**
- [ ] Partition tháng sau **tự động tồn tại**, đã test bằng cách chỉnh đồng hồ

**Scale** 🆕
- [ ] **LD-1…LD-4 đạt mục tiêu** p95
- [ ] Nạp **50 triệu dòng** ledger, chứng minh partition pruning hoạt động
- [ ] Nạp dữ liệu **T2 (~256 Tr dòng fact)** vào ClickHouse, truy vấn lớp C < 3 s
- [ ] 200 kết nối đồng thời tới trung tâm: không lỗi, hoặc 503 tử tế
- [ ] Số liệu đo thật đã ghi ngược vào [02-scale-capacity.md](02-scale-capacity.md)

---

## MoSCoW — cái gì rơi trước

Khi thời gian cạn (sẽ cạn), cắt theo thứ tự này. **Cắt từ dưới lên.**

> ⚠️ **Đã sắp xếp lại theo ưu tiên "ổn định + scale"** (chủ dự án, 2026-09-11). Nhiều hạng
> mục **verification** được nâng lên Must, nhiều **tính năng** bị hạ xuống Could. Cơ sở:
> [08-reliability-and-scale.md §7](08-reliability-and-scale.md).

| Ưu tiên | Hạng mục |
|---|---|
| **Must — Nghiệp vụ lõi** | Đăng nhập · bán hàng · ledger điểm · outbox + sync · idempotency · **báo cáo vận hành T+0** · bronze → warehouse · dim + fact |
| **Must — Ổn định** 🆕 | **Observability** (metrics + log JSON + cảnh báo) · **tự động tạo partition** · **backpressure + rate limit** · **backup + khôi phục cửa hàng** · **đối soát tăng dần** · theo dõi sync theo cửa hàng |
| **Must — Chứng minh** 🆕 | AT-01…AT-10 · **test hỗn loạn CH-1…CH-7** · **test ngâm 72h** · **test tải LD-1…LD-4** · test toàn vẹn DI-1…DI-5 |
| **Should** | Trả hàng · backfill · schema bổ sung (`sale_payment`, `shift`) |
| **Could** ⬇️ | Trừ tồn kho · đối soát tiền khi chốt ca · SCD2 · Metabase *(thay bằng SQL mẫu)* · in hóa đơn · điểm hết hạn |
| **Không làm (đã chốt)** | Redeem điểm · multi-tenant — [ADR-008](adr/008-remaining-decisions.md) |

**Đánh đổi có chủ đích:** ít tính năng hơn, nhiều bằng chứng hơn. Một hệ thống làm ít việc mà
chứng minh được là chạy ổn định dưới tải và khi hỏng hóc, có giá trị hơn một hệ thống làm
nhiều việc mà không ai biết nó chịu được đến đâu.
| **Không làm** | Iceberg · Kafka · k8s · microservices · multi-region · đa tiền tệ |

## Rủi ro đã nhận diện

| Rủi ro | Xác suất | Giảm thiểu |
|---|---|---|
| Tuần 2 tràn sang tuần 3 | **Cao** | Đã dự tính. Tuần 3 có phương án tối thiểu (bỏ silver) |
| Học ClickHouse/dbt/Dagster tốn hơn dự kiến | Trung bình | Mỗi cái ~1 ngày. Nếu Dagster cản đường → tạm dùng cron + `dbt build`, orchestrator là lớp vỏ ([ADR-006](adr/006-dagster-over-airflow.md)) |
| Debug race condition ở AT-04 tốn nhiều ngày | Trung bình | Ledger được chọn chính xác để **loại bỏ** race condition, không phải để quản lý nó. Nếu vẫn đau → xem lại có chỗ nào đang `UPDATE` số dư |
| Lỗi Docker trên Windows (đường dẫn, volume, quyền) | Trung bình | **Không mount volume DB vào OneDrive.** Dùng named volume, không bind mount |
| Phạm vi trôi sang tính năng đẹp mà không cốt lõi | Cao | Bảng MoSCoW ở trên là hợp đồng với chính mình |

## Sau 4 tuần — backlog v2

Theo thứ tự ưu tiên khi POC được chấp nhận:

1. Nâng Parquet → **Iceberg** (schema evolution, time travel)
2. Outbox → **CDC + Redpanda** khi vượt ~100 cửa hàng ([ADR-003](adr/003-outbox-not-kafka.md))
3. **PgBouncer** khi vượt ~50 cửa hàng
4. Redeem điểm, điểm hết hạn, chiến dịch điểm thưởng
5. Kiểm kho, chuyển kho, đặt hàng nhà cung cấp
6. Warehouse near-realtime (dùng lại luồng CDC)
7. Deploy production: TLS, secret manager, backup tự động, monitoring
8. Tách microservice **nếu** team lớn lên ([ADR-004](adr/004-modular-monolith.md))

---
*Changelog: 2026-09-11 — tạo mới.*
