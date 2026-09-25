# 06 — Lộ trình

**Ngân sách:** 1 người, ~4 tuần, ~80–120 giờ làm việc thật, có Claude Code hỗ trợ.

Lộ trình này **cố tình cắt sâu**. Thà có một luồng end-to-end chạy thật còn hơn năm luồng
dở dang. Mỗi tuần/giai đoạn có một **cổng kiểm tra** — không qua cổng thì không đi tiếp, phải
cắt phạm vi thay vì cắt chất lượng.

> 🔀 **Từ 2026-09-23 lộ trình đổi hướng** theo [ADR-010](adr/010-data-flow-first.md): luồng dữ
> liệu trước, tính năng sau. Tuần 1 và nửa đầu tuần 2 giữ nguyên làm hồ sơ; phần còn lại là
> giai đoạn A → B → C.

---

> **Tiền đề:** stack đã chốt toàn bộ ([07-stack-decision.md](07-stack-decision.md)) nhưng
> **chưa kiểm chứng thực nghiệm**. Ngày 0 dành cho spike xác minh — xem dưới.

## Ngày 0 — Spike xác minh *(nửa ngày, làm trước tuần 1)*

Chốt trên giấy xong rồi, nhưng mọi con số vẫn là ước lượng. Nửa ngày này để phát hiện sai
lầm ở ngày 0 thay vì ngày 15.

| # | Kiểm chứng | Đạt khi | Trạng thái |
|---|---|---|---|
| S1 | `compose.yaml` rỗng: PG 18 + Airflow 3 LocalExecutor + ClickHouse + MinIO | Khởi động hết, **đo RAM thật** ≤ 8 GB | ✅ 2026-09-24 — `edge`+`central`+`data` (12 container, Airflow 3.3.2): **2,8 GiB** lúc nghỉ, **đỉnh 3,9 GiB** giữa lượt DAG (scheduler 2,1 GiB khi các task dbt chạy song song). `observability` thêm ~0,5 GiB (đo 2026-09-18) |
| S2 | Migration Alembic dùng `uuidv7()` qua SQLAlchemy 2 async | Sinh được ID, driver không lỗi | ✅ Đạt — `uuidv7()` DEFAULT chạy thật trên PG 18.6, xác nhận sắp theo thời gian (`test_uuidv7_is_time_ordered`) |
| S3 | DAG rỗng chạy dbt qua `astronomer-cosmos` | Thấy từng dbt model là một task riêng, có lineage | ✅ 2026-09-24 — cosmos 1.15: 22 model → task `run`+`test`, test nhiều cha thành task riêng. Ba cấu hình bắt buộc: `InvocationMode.SUBPROCESS` (dbt ở venv riêng), `should_detach_multiple_parents_tests`, `source_rendering_behavior` (nếu không, test trên source bị bỏ) |
| S4 | Nạp `crawl_data/products/` vào ClickHouse bằng hàm `s3()` từ MinIO | Đếm đúng số dòng | ✅ 2026-09-24 — `insert_deduplication_token` kiểm trên đúng image (`test_clickhouse_bronze.py`); `s3()` từ MinIO chạy thật trong `packages/pipeline` (5556 sản phẩm, chạy lại không nhân đôi) |
| S5 | `grafana/otel-lgtm` lên, FastAPI rỗng gửi trace qua OTLP | Thấy trace trong Grafana/Tempo; **đo RAM thật** của container này | ✅ Đạt (2026-09-18) — trace `GET /health` của cả `edge-api` lẫn `central-api` thấy được qua Tempo API, kèm span con `asyncpg`/`redis` lồng đúng. RAM `otel-lgtm` lúc khởi động: **~414 MiB** |

**Nếu S1 trượt** (RAM vượt): giảm còn 2 cửa hàng mô phỏng, hoặc tách profile chặt hơn.
**Nếu S3 trượt:** dùng `BashOperator` chạy `dbt build` — mất lineage nhưng không chặn.
**Nếu S2 trượt:** sinh UUIDv7 ở tầng Python (`uuid6` package) thay vì trong DB.

Không cái nào trong bốn cái này đòi đổi quyết định stack — chỉ đổi cách triển khai.

## Nguyên tắc phân bổ thời gian

Claude Code tăng tốc rất mạnh ở: sinh schema, CRUD, dbt model, docker config, test,
migration, docs. Tăng tốc rất ít ở: debug race condition, tinh chỉnh consistency, dựng và
gỡ lỗi hạ tầng, quyết định thiết kế.

→ Dồn thời gian người vào **đồng bộ** và **giai đoạn B** (hỗn loạn, dữ liệu đến trễ, số đo). Để
Claude Code làm nặng ở schema, dbt model, bộ giả lập, docker config.

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

## Tuần 2 (phần đã làm, tới 2026-09-23) — Loyalty + Đồng bộ

Làm theo lộ trình cũ cho tới 2026-09-23. Phần đã xong đều nằm trên luồng dữ liệu nên được
giữ nguyên. Chi tiết ở [docs/progress/2026-09-23-tuan-2-dong-bo.md](progress/2026-09-23-tuan-2-dong-bo.md).

| Việc | Trạng thái |
|---|---|
| Module loyalty: ledger cục bộ, `lifetime_earned`, xếp hạng, `LoyaltyPort` + `import-linter` | ✅ (tuần 1) |
| Một transaction cho sale + line + payment + ledger + outbox | ✅ (tuần 1) |
| Central `POST /events`: SAVEPOINT từng sự kiện, `retryable`, xác thực khóa cửa hàng, backpressure 413/503 | ✅ |
| Sync worker: `SKIP LOCKED`, lô, backoff riêng từng sự kiện, **lỗi đường truyền không tăng `attempts`** | ✅ |
| `traceparent` qua outbox + span link | ✅ |
| AT-01, AT-02, AT-03, AT-04 (phần trung tâm) ở mức tích hợp; AT-02 chạy tay trên compose | ✅ |

---

## 🔀 Từ 2026-09-23 — đổi hướng: luồng dữ liệu trước, tính năng sau

Chủ dự án quyết định ([ADR-010](adr/010-data-flow-first.md)): **chỉ tập trung vào luồng dữ
liệu, không dựng tiếp ứng dụng để dùng.** Dữ liệu vào hệ thống bằng **bộ giả lập**
([18](18-simulator.md)) thay vì UI. Tính năng chỉ làm sau khi luồng đã được chứng minh ổn định.

Phần còn lại của tuần 2 và toàn bộ tuần 3–4 cũ được sắp lại thành ba giai đoạn:

```
Giai đoạn A — Luồng dữ liệu thông suốt     ngày 9–14    →  🚪 Cổng A
Giai đoạn B — Chứng minh ổn định & scale    ngày 15–20   →  🚪 Cổng B = nghiệm thu POC
Giai đoạn C — Tính năng                      sau cổng B    (ngoài ngân sách 1 tháng)
```

**Quy tắc chuyển giai đoạn:** không qua cổng thì không sang giai đoạn sau. Hết thời gian thì
cắt phạm vi theo MoSCoW bên dưới, **không** nhảy sang C để "có cái demo".

---

## Giai đoạn A — Luồng dữ liệu thông suốt ⭐

**Mục tiêu:** một đơn bán ra ở cửa hàng đi tới `fact_sale_line` trong ClickHouse, **đúng và
đủ** ở mọi tầng, chạy lại bất kỳ chặng nào cũng không nhân đôi. Bản đồ luồng và 5 bẫy phải xử
lý: [17-data-flow](17-data-flow.md).

| Ngày | Việc | Ghi chú |
|---|---|---|
| 9 | ✅ **Đường ghi còn thiếu cho bộ giả lập** (use case + route JSON, **không UI**): `RegisterCustomer` + `POST /customers`, `OpenShift` + `POST /shifts/open` (route tạm `/ui/shifts/open` giờ gọi cùng use case), `CloseShift` + `POST /shifts/{id}/close`, seed nhân viên | Xong 2026-09-23. `CloseShift` nâng lên **Must**: không đóng ca thì `business_date` kẹt mãi ([ADR-010](adr/010-data-flow-first.md) changelog) |
| 10 | ✅ **Bộ giả lập** bước 1–3 ([18 §10](18-simulator.md)): `generator` + profile `t0` + sink `edge_http` + manifest + `audit` L1/L2, kèm `POST /sales/quote` | Xong 2026-09-23 — `CONVERGED` trong test và trên compose |
| 10 | ✅ Job đối soát INV-4 **tăng dần** (`central.ops.reconcile`), dùng đúng `lifetime_contribution`, lệch lưu ở `reconciliation_drift` | Xong 2026-09-23 (ràng buộc #8, AT-10) |
| 11 | ~~**Sửa trước 3 bẫy** ở schema trung tâm~~ | ✅ **Làm sớm (2026-09-23)**, cùng bẫy 4: migration `0004`, `extract_horizon()`, `transaction_timeout`, DDL bronze. [17 §4](17-data-flow.md) |
| 11 | ✅ Profile `data` lên compose: MinIO + ClickHouse + **Airflow 3.3.2** (api-server, scheduler, dag-processor; không triggerer). **Spike S1, S3, S4 ✅** | Xong 2026-09-24. Sửa compose cũ: `webserver` → `api-server`, thêm `dag-processor`, JWT chung + `execution_api_server_url` (thiếu thì task chết "Signature verification failed") |
| 12 | ✅ S4 **trích xuất** trung tâm → bronze (`packages/pipeline`, lake bất biến là trạng thái, mép cửa sổ ≤ `extract_horizon()`) | Xong 2026-09-24 (ràng buộc #3) |
| 12 | ✅ S5 **nạp** bronze → `bronze_*` + `bronze_load_log`, token = đường dẫn + SHA-256 | Xong 2026-09-24 (ràng buộc #4). Audit L3 cũng xong |
| 13 | ✅ S6 **dbt**: staging (vai trò silver) → `dim_date`, `dim_store`, `dim_product`, `dim_employee`, `dim_customer` (SCD1, **không PII**), `dim_shift` → `fact_sale_line`, **`fact_payment` riêng**, `fact_point_event` + 32 test | Xong 2026-09-24. Bẫy 5 ✅ (`insert_overwrite` theo tập tháng bị ảnh hưởng). Dim có inferred member cho master data lệch |
| 13 | ✅ DAG Airflow nối S4 → S5 → S6 (cosmos), chu kỳ `PIPELINE_SCHEDULE` | Xong 2026-09-24. Chạy thật trên compose, mỗi lượt ~2,5–3 phút |
| 14 | ✅ Bộ giả lập bước 4–5: sink `virtual` + tật `offline`/`resend`/`concurrent_customer` + `audit` L3/L4 | Xong 2026-09-24. 20 cửa hàng ảo trên compose → `CONVERGED` L0…L4 |
| 14 | Dashboard "sức khỏe luồng" ([17 §6](17-data-flow.md)): trễ theo chặng, độ tươi, chênh đối soát | Must — **chưa làm**, không thuộc 8 điều kiện cổng A; dời thành việc ĐẦU TIÊN của giai đoạn B (test ngâm/hỗn loạn cần nó để quan sát) |
| 14 | *Should:* trả hàng: `ReturnItems` (không UI), mở rộng payload `SaleReturned`, handler trung tâm, `is_return` trong fact | Sinh dòng âm cho ledger/fact. AT-06 |

### 🚪 Cổng A — ✅ ĐẠT 2026-09-24 ([progress](progress/2026-09-24-cong-a.md))

- [x] Bộ giả lập `edge` + profile `t0` (3 cửa hàng) chạy **2 giờ liên tục** → `audit` = `CONVERGED` ở L0…L4 *(2026-09-24 07:52→09:52: 602 đơn, 12 ca, DAG chạy 10 phút/lần suốt giờ bán — 13 lượt, không lượt nào đỏ; 0 đơn không rõ kết cục; RAM đi ngang ~3,4 GiB)*
- [x] Bộ giả lập `virtual` 20 cửa hàng, bật tật `offline` + `resend` + `concurrent_customer` → `CONVERGED` *(2026-09-24: 19.930 đơn / 31.904 sự kiện, 479 lô gửi lại, 563 đơn mua chéo; L0…L4 khớp từng cửa hàng, từng ngày; 0 dead-letter; INV-4 drift 0)*
- [x] **AT-07** — chạy DAG hai lần liên tiếp, mọi số ở [17 §5](17-data-flow.md) không đổi *(2026-09-24: 6 lượt DAG trên compose, không phân vùng fact nào bị thay khi không có dòng mới)*
- [x] Xóa sạch ClickHouse, dựng lại từ bronze → số khớp như trước khi xóa *(2026-09-24: `DROP DATABASE dw` → một lượt DAG → 13 con số khớp tuyệt đối, audit 3 lần chạy vẫn `CONVERGED` L0…L4)*
- [x] `SUM(fact_payment.amount)` theo ngày = `SUM(fact_sale_line.net_amount)` — không nhân đôi *(test dbt `assert_payments_equal_net_sales_per_day`. Bản đầu ghi `line_total`, sai vì thanh toán bằng tổng SAU chiết khấu)*
- [x] Offline vắt qua cuối tháng → phân vùng tháng trước được tính lại đúng (bẫy 5) *(test tích hợp trên ClickHouse thật; **và trên compose** 2026-09-24: mart 31/8 của cửa hàng offline 164 → 308/308 đơn sau khi nó đồng bộ bù)*
- [x] **AT-10** — đối soát INV-4 khớp tuyệt đối; `dbt test` xanh toàn bộ *(2026-09-24, sau lượt `virtual` 20 cửa hàng: reconcile 649 khách drift 0; DAG `success`, chỉ còn 2 cảnh báo dữ liệu demo tuần 1)*
- [x] AT-01…04 chuyển thành test tự động chạy bằng bộ giả lập (`tests/scenarios/`) *(2026-09-24: `test_at.py` + `test_data_integrity.py` (AT-07, AT-10, DI-1…3), 7/7 trên compose thật; opt-in `make test-scenarios` vì AT-02 tắt Central API. CI chưa có job compose — giai đoạn B)*

**Nếu trượt:** cắt trả hàng (Should), dùng `dim_customer` tối giản. **Không
cắt** đối soát xuyên tầng hay AT-07: không có hai thứ đó thì cổng A không chứng minh được gì.

---

## Giai đoạn B — Chứng minh ổn định & scale ⭐

**Mục tiêu:** **chứng minh** luồng ở giai đoạn A chịu được tải và hỏng hóc. Không phải tuyên
bố. Mọi thử nghiệm dùng bộ giả lập làm nguồn tải và `audit` làm điều kiện đạt. Chi tiết từng
thử nghiệm ở [08 §6](08-reliability-and-scale.md).

| Ngày | Việc | Ghi chú |
|---|---|---|
| 15 | ✅ **Nợ từ A:** dashboard "sức khỏe luồng" ([17 §6](17-data-flow.md)) — độ tươi L2/L4, trễ theo chặng, chênh đối soát; audit báo độ tươi | Xong 2026-09-25 ([progress](progress/2026-09-25-giai-doan-b-dashboard.md)): metric OTel ở edge-api/sync worker/central-api, bộ giám sát `pipeline monitor`, dashboard provision từ repo, `audit --watch --otlp`. Kèm: job INV-4 lên lịch (`central-reconcile`), sửa `/health/outbox` đếm cả dead-letter, mật khẩu ClickHouse ra khỏi URL |
| 15 | ✅ **Nợ từ A:** job CI dựng compose + seed, chạy `tests/scenarios/` mỗi PR ([16 §6](16-test-plan.md)) | Xong 2026-09-25 ([progress](progress/2026-09-25-giai-doan-b-ci.md)): `infra/bootstrap.py` (dựng từ con số 0, chạy lại được) + job `scenarios`; dữ liệu master tổng hợp `tests/fixtures/crawl_data/` vì `crawl_data/` thật không nằm trong git |
| 15 | **Test hỗn loạn CH-1…CH-7** trên luồng đang chạy: ngắt mạng, `kill -9` app, `kill -9` Postgres, đầy đĩa, tắt Redis, 10 CH nối lại cùng lúc, gửi lặp | 🔴 Must — sau mỗi kịch bản `audit` phải về `CONVERGED` |
| 16 | Bộ giả lập bước 6–7: sink `bulk` + test hợp đồng schema bronze, profile `t2`/`t3` | Must |
| 16 | **Test tải LD-1…LD-4** bằng bộ giả lập (vòng hở) | 🔴 Must. Thay `k6` — [18 §9](18-simulator.md) |
| 17 | **Kiểm chứng scale seam:** 50 triệu dòng `point_ledger` + đo partition pruning; dữ liệu T2 (~256 Tr dòng fact) vào ClickHouse, truy vấn lớp C < 3 s; 200 kết nối đồng thời | 🔴 Must — [08 §4.4](08-reliability-and-scale.md). Ghi số đo thật vào [02](02-scale-capacity.md) |
| 17 | Backfill 7 ngày quá khứ (FR-C09) | Should |
| 18 | **Test ngâm 72h** chạy nền (`edge` t0 + `virtual` t1): rò rỉ bộ nhớ, bloat `outbox`, trôi độ trễ, `audit` định kỳ | 🔴 Must — chạy nền trong ngày 18–20 |
| 18 | **Đo overhead OTel:** chạy LD-1 hai lần, có và không có instrumentation | 🔴 Must — > 5% thì giảm tỉ lệ lấy mẫu ([ADR-009](adr/009-observability-stack.md)) |
| 19 | Ngưỡng cảnh báo [08 §5](08-reliability-and-scale.md) cấu hình xong **và kích hoạt thử được**; `structlog` JSON | 🔴 Must — 2026-09-25: ✅ 15 rule provision từ repo (đánh giá được, `inactive`), ✅ log JSON + `trace_id`. ⏳ **kích hoạt thử** từng rule (cùng test hỗn loạn) · chưa có contact point |
| 19 | Partition tháng sau **tự tồn tại**, test bằng cách chỉnh đồng hồ; job dọn `outbox` đã gửi > 7 ngày | 🔴 Must — 2026-09-25: ✅ lịch `central-maintenance` (trước đó **không có gì gọi** hàm tạo partition — xem [progress](progress/2026-09-25-giai-doan-b-van-hanh.md)), ✅ dọn outbox trong sync worker. ⏳ thử bằng chỉnh đồng hồ |
| 20 | Backup + khôi phục Postgres cửa hàng (quy trình, chạy thử một lần) | Must — 2026-09-25: ✅ sidecar `edge-backup-*` (hằng ngày, giữ 7), ✅ `store_backup.py verify` đã chạy (khôi phục vào DB tạm, 7 bảng khớp). ⏳ `restore` thật trên DB cửa hàng chưa chạy thử |
| 20 | Cập nhật docs bằng số liệu đo thật; README chạy thử từ `git clone` | |

### 🚪 Cổng B (nghiệm thu POC)

**Ổn định**
- [ ] **CH-1…CH-7 đều đạt**, sau mỗi kịch bản `audit` = `CONVERGED`. Đặc biệt CH-3 (`kill -9` Postgres → 0 giao dịch đã commit bị mất)
- [ ] **Test ngâm 72h:** bộ nhớ không rò rỉ, `outbox` không bloat, p95 không trôi, `audit` không lần nào `DIVERGED`
- [ ] **DI-1…DI-5** đều xanh
- [ ] Mọi ngưỡng cảnh báo đã cấu hình và **đã kích hoạt thử được**
- [ ] Partition tháng sau tự tồn tại

**Scale**
- [ ] **LD-1…LD-4 đạt mục tiêu** p95, bộ giả lập < 50% CPU trong mọi phép đo
- [ ] 50 triệu dòng ledger, partition pruning được chứng minh bằng `EXPLAIN`
- [ ] Dữ liệu T2 trong ClickHouse, truy vấn lớp C < 3 s
- [ ] 200 kết nối đồng thời tới trung tâm: không lỗi, hoặc 503 tử tế
- [ ] Số đo thật đã ghi ngược vào [02-scale-capacity.md](02-scale-capacity.md)

**Vận hành**
- [ ] `git clone` → `docker compose up` → bộ giả lập chạy được theo README

---

## Giai đoạn C — Tính năng *(sau cổng B, ngoài ngân sách 1 tháng)*

Chạy trên một luồng đã được chứng minh. Bộ test hỗn loạn/tải của B là lưới an toàn cho mọi
thay đổi ở đây. Xếp theo giá trị cho người dùng tại quầy:

1. **Tra khách 3 bước** (Redis → Postgres cửa hàng → trung tâm 500ms) + làm mới cache khi đồng
   bộ. Trung tâm cần `GET /customers/*`. AT-05
2. **Báo cáo vận hành lớp A:** báo cáo ca, doanh thu hôm nay, theo thu ngân (FR-R01…R03)
3. UI: đăng ký khách, trả hàng (nếu chưa làm ở A-Should: cả phần nghiệp vụ), đóng ca
4. **Kéo master data** có phiên bản xuống cửa hàng (FR-C01)
5. `GET /sync/status` cho trụ sở (FR-C10). Cần chốt cách xác thực người dùng trụ sở
6. Lên hạng áp dụng ngay đơn sau (AT-09) đi qua trung tâm
7. Metabase + 4 dashboard nghiệp vụ (FR-C08)
8. SCD2 cho `dim_customer`/`dim_product`, mask PII + nhật ký truy cập PII (NFR-06)
9. Trừ tồn kho, đối soát tiền chốt ca, in hóa đơn, điểm hết hạn

---

## MoSCoW — cái gì rơi trước

Khi thời gian cạn (sẽ cạn), cắt theo thứ tự này. **Cắt từ dưới lên.**

> ⚠️ **Sắp xếp lại lần hai (2026-09-23, [ADR-010](adr/010-data-flow-first.md)).** Lần một
> (2026-09-11) nâng verification lên Must. Lần này hạ mọi tính năng dùng tại quầy xuống
> **giai đoạn C**. Trong ngân sách 1 tháng, chỉ còn luồng dữ liệu và bằng chứng.

| Ưu tiên | Hạng mục |
|---|---|
| **Must — Luồng dữ liệu (A)** | Đường ghi cửa hàng (bán, đăng ký khách, mở/đóng ca) · outbox + sync · idempotency · trích xuất → bronze → ClickHouse → dbt → Airflow · dim + fact · **bộ giả lập `edge` + `virtual`** · **đối soát xuyên tầng** · đối soát INV-4 tăng dần |
| **Must — Ổn định (B)** | Observability (metrics + log JSON + cảnh báo + dashboard sức khỏe luồng) · tự tạo partition · backpressure · backup + khôi phục cửa hàng · dọn `outbox` |
| **Must — Chứng minh (B)** | AT-01…04, 07, 08, 10 · **CH-1…CH-7** · **test ngâm 72h** · **LD-1…LD-4** · DI-1…DI-5 · scale seam bằng số đo · bộ giả lập `bulk` |
| **Should** | Trả hàng + AT-06 · backfill · profile `t3` |
| **C — sau cổng B** | Tra khách 3 bước (AT-05) · báo cáo lớp A · UI mới · kéo master data · `GET /sync/status` · AT-09 · Metabase · SCD2 · mask PII nâng cao · trừ tồn kho · đối soát tiền ca · in hóa đơn · điểm hết hạn |
| **Không làm (đã chốt)** | Redeem điểm · multi-tenant ([ADR-008](adr/008-remaining-decisions.md)) · silver dạng Parquet (v2, [17 §2](17-data-flow.md)) |
| **Không làm** | Iceberg · Kafka · k8s · microservices · multi-region · đa tiền tệ |

**Đánh đổi có chủ đích:** ít tính năng hơn, nhiều bằng chứng hơn. Một hệ thống làm ít việc mà
chứng minh được là chạy ổn định dưới tải và khi hỏng hóc, có giá trị hơn một hệ thống làm
nhiều việc mà không ai biết nó chịu được đến đâu.

## Rủi ro đã nhận diện

| Rủi ro | Xác suất | Giảm thiểu |
|---|---|---|
| Giai đoạn A tràn sang B | **Cao** | Cắt Should trước. Phần B tối thiểu: CH-1/2/3/7 + ngâm 24h thay 72h + LD-2 |
| Học ClickHouse/dbt/Airflow tốn hơn dự kiến | Trung bình | Spike S3/S4 làm **đầu** ngày 11. Nếu cosmos cản đường → `BashOperator` chạy `dbt build` (mất lineage, không chặn) |
| Bộ giả lập sinh dữ liệu quá sạch | Trung bình | Tật cấu hình được ([18 §5](18-simulator.md)), bật mặc định trong cổng A |
| Bộ giả lập tự thành nút thắt, làm sai số đo tải | Thấp | Vòng hở + quy tắc < 50% CPU ([18 §1](18-simulator.md)) |
| Phạm vi trôi sang tính năng | **Cao** | [ADR-010](adr/010-data-flow-first.md) quy tắc 2: chỉ thêm code tính năng khi luồng thiếu một loại dữ liệu |
| Lỗi Docker trên Windows (đường dẫn, volume, quyền) | Trung bình | **Không mount volume DB vào OneDrive.** Dùng named volume, không bind mount |

## Sau POC — backlog v2

Theo thứ tự ưu tiên khi POC được chấp nhận (sau giai đoạn C):

1. Nâng Parquet → **Iceberg** (schema evolution, time travel); silver dạng Parquet nếu có consumer thứ hai
2. Outbox → **CDC + Redpanda** khi vượt ~100 cửa hàng ([ADR-003](adr/003-outbox-not-kafka.md))
3. **PgBouncer** khi vượt ~50 cửa hàng
4. Redeem điểm, điểm hết hạn, chiến dịch điểm thưởng
5. Kiểm kho, chuyển kho, đặt hàng nhà cung cấp
6. Warehouse near-realtime (dùng lại luồng CDC)
7. Deploy production: TLS, secret manager, backup tự động, monitoring
8. Tách microservice **nếu** team lớn lên ([ADR-004](adr/004-modular-monolith.md))

---
*Changelog: 2026-09-25 — giai đoạn B ngày 15: dashboard "sức khỏe luồng" xong.*

*Changelog: 2026-09-24 (lần 3) — **cổng A đạt**: 3 cửa hàng `edge` chạy 2 giờ `CONVERGED` L0…L4,
`tests/scenarios/` AT-01…04 + AT-07/10 xanh trên compose. Dashboard sức khỏe luồng (Must ngày 14)
và trả hàng (Should) chưa làm — dashboard dời lên đầu giai đoạn B.*

*Changelog: 2026-09-24 (lần 2) — ngày 14 bước bộ giả lập xong; cổng A tick 6/8 (thêm `virtual`
20 cửa hàng, AT-10).*

*Changelog: 2026-09-24 — ngày 11 và 13 xong (Airflow 3, dbt S6, DAG); spike S1, S3 ✅ kèm số
đo; cổng A tick 4/8 mục, sửa mục "Σ `line_total`" thành "Σ `net_amount`".*

*Changelog: 2026-09-23 — viết lại từ tuần 2 trở đi theo [ADR-010](adr/010-data-flow-first.md):
ba giai đoạn A (luồng dữ liệu) → B (chứng minh) → C (tính năng); bộ giả lập kéo từ ngày 16 lên
ngày 10; `k6` thay bằng bộ giả lập; silver Parquet để v2. Giữ nguyên phần tuần 1 và spike.*

*Changelog: 2026-09-11 — tạo mới.*
