# retail-sync

Hệ thống dữ liệu cho chuỗi cửa hàng bán lẻ: POS (đơn hàng + khách hàng) và Loyalty (tích điểm),
kèm tầng phân tích trung tâm.

## ⚠️ Đọc trước khi làm bất cứ việc gì

Thiết kế v2 **đã hoàn tất**; đang **viết code v2** trong `packages/`.
**Tuần 1 ([lộ trình](docs/06-roadmap.md)) đã đóng cổng (2026-09-18)** — xem tổng kết ở
[docs/progress/2026-09-18-tong-ket-tuan-1.md](docs/progress/2026-09-18-tong-ket-tuan-1.md)
(đã làm gì, đã kiểm chứng ra sao, demo được gì, còn thiếu gì). Từ 2026-09-23 lộ trình đi theo
giai đoạn A/B/C (ADR-010) thay cho tuần 2–4. Giai đoạn A: **luồng S1 → S6 chạy hết trên
compose**. **🚪 Cổng A ĐẠT (2026-09-24)**, cả 8 điều kiện kiểm trên compose thật, xem
[docs/progress/2026-09-24-cong-a.md](docs/progress/2026-09-24-cong-a.md). **Giai đoạn B** đang chạy
([06](docs/06-roadmap.md)): ✅ dashboard "sức khỏe luồng" (2026-09-25,
[nhật ký](docs/progress/2026-09-25-giai-doan-b-dashboard.md)), ✅ job CI `scenarios` +
`infra/bootstrap.py` ([nhật ký](docs/progress/2026-09-25-giai-doan-b-ci.md)), ✅ vá vận hành: lịch
partition + INV-4, dọn outbox, log JSON, 15 cảnh báo, backup cửa hàng
([nhật ký](docs/progress/2026-09-25-giai-doan-b-van-hanh.md)), ✅ **CH-1…CH-7 đều đạt**, 21 cảnh báo (16
đã kích hoạt thử), partition "chỉnh đồng hồ", dữ liệu T2 đã sinh
([nhật ký](docs/progress/2026-09-25-giai-doan-b-chung-minh.md) — **đọc §8 "điểm dừng"**). Chủ dự án
muốn **xong B rồi mới chạy test ngâm 72h**, và **không muốn máy cá nhân chạy nhiều giờ** → diễn tập,
LD-1/2/4, `restore` thật chạy bằng **workflow `proof`** trên GitHub Actions (bấm tay, mỗi nhóm một runner,
≤ 6 giờ — [nhật ký](docs/progress/2026-09-28-giai-doan-b-proof-ci.md), **đọc §5 trạng thái + §6 các bước
tiếp theo**). 2026-10-08: **cảnh báo 21/21** ✅, `restore` thật ✅, LD-1/LD-4 ✅, **trace mặc định 10%**
(ADR-009); LD-2 sau ADR-011 đạt ≈ tải thiết kế, tiêu chí hai vùng (tới / vượt tải thiết kế) chờ chạy lại. Còn cần máy chạy liên tục: LD-3, seam 50 triệu dòng ledger, test ngâm.

## 🔀 Hướng phát triển hiện tại (chủ dự án, 2026-09-23) — [ADR-010](docs/adr/010-data-flow-first.md)

> **Chỉ tập trung vào luồng dữ liệu.** Không dựng tiếp ứng dụng để dùng. Dữ liệu vào bằng
> **bộ giả lập** ([docs/18](docs/18-simulator.md)), không qua UI. Chứng minh ổn định xong mới
> làm tính năng.

**A** luồng dữ liệu thông suốt ([docs/17](docs/17-data-flow.md)) → **B** chứng minh ổn định &
scale → **C** tính năng. Không qua cổng thì không sang giai đoạn sau
([docs/06](docs/06-roadmap.md)). Hệ quả khi làm việc:
- **Không viết UI mới** trong A/B. UI POS tuần 1 đóng băng: giữ, không mở rộng.
- Chỉ thêm code tính năng khi **thiếu nó thì luồng thiếu một loại dữ liệu**. Code thêm là use
  case + route JSON, không kèm UI. Ví dụ đúng: `POST /customers`. Ví dụ sai: tra khách 3 bước.
- Bộ giả lập ở chế độ `edge` **đi qua Edge API thật**, không `INSERT` thẳng vào bảng giao dịch
  cửa hàng. Dữ liệu `bulk` chỉ để đo khối lượng, không dùng làm bằng chứng đúng.
- Người dùng hỏi tính năng C (tra khách, báo cáo ca, dashboard...) → nhắc là đang ở giai đoạn
  nào, trỏ về ADR-010, đừng tự làm.
- Trước khi viết trích xuất/nạp ClickHouse: đọc **5 bẫy ở [docs/17 §4](docs/17-data-flow.md)**.
  Bẫy 1–4 đã sửa (2026-09-23) — dùng lại cách sửa, đừng viết lại: mép cửa sổ trích xuất là
  `extract_horizon()`, không phải `now()`; watermark do **trigger** đặt, handler không cần set;
  mọi bảng `bronze_*` phải có `non_replicated_deduplication_window` (test đỏ nếu quên); token
  nạp ClickHouse = đường dẫn **+ SHA-256 nội dung**.
- **Lake bất biến:** file bronze đã ghi thì không bao giờ trích lại. Cửa sổ mới bắt đầu ở mép
  cuối của file cuối cùng, nên chính lake là trạng thái, không có bảng watermark riêng.
- MinIO: **`cgr.dev/chainguard/minio@sha256:…`** (ghim DIGEST — bản miễn phí chỉ có `latest`).
  Docker Hub `minio/minio` đã xóa, `quay.io/minio/minio` bắt đăng nhập (2026-09-28, CI đỏ) — máy dev
  chạy được chỉ nhờ cache. Image distroless: không `mc`/healthcheck, chạy user 0. Đổi digest ở
  compose VÀ `tests/integration/conftest.py` cùng lúc (ADR-005 §Rủi ro nguồn cung).
- **Một lượt pipeline mỗi lúc:** `run_once` giữ advisory lock trên PG trung tâm qua CẢ trích
  lẫn nạp (DAG và `make pipeline-run` có thể chồng nhau). Bảng incremental rỗng vẫn ghi một
  file mốc, vì mép "đã nạp tới" đòi đủ 7 bảng.
- **dbt (S6), đọc [data_platform/README.md](data_platform/README.md) trước khi thêm model:** fact
  tăng dần = `insert_overwrite` + `affected_months()` + `pipeline_cutoff()` (bẫy 5). Staging
  lọc `loaded_until()`. Mart dùng `MergeTree`, **không** `ReplacingMergeTree` (nó che nhân đôi
  khỏi audit L4). Dim có inferred member. **Bẫy alias ClickHouse:** `f(x) AS x ... WHERE x` kiểm
  alias chứ không kiểm cột.
- **Partition `point_ledger` giữ cả tháng trước** (migration `0006`, `months_back`): cửa hàng
  offline qua cuối tháng đồng bộ ngày 1. Ở tháng go-live partition đó không có sẵn, và điểm vào
  dead-letter không thử lại. Đừng bỏ `months_back`.
- **Ca chỉ lên trung tâm khi đóng** → `dim_shift` có dòng inferred cho ca đang mở. DAG chạy giữa
  giờ bán là bình thường, đừng biến nó thành test đỏ.
- **Bộ giả lập `virtual`** ([docs/18 §3](docs/18-simulator.md)) được import ĐÚNG 4 module thuần của
  edge (`edge.sync.client`, `edge.sync.backoff`, `edge.pos.domain`, `edge.loyalty.domain`). Thêm
  import DB/FastAPI vào chúng là test tính thuần đỏ. Khóa cửa hàng ảo ở `runs/virtual-keys.env`
  là SECRET (gitignore).
- **Dựng stack: `uv run python infra/bootstrap.py`** (migrate, seed, khóa, lake, chờ DAG — chạy lại
  được, giữ khóa còn hợp lệ). `crawl_data/` thật KHÔNG nằm trong git → clone sạch/CI dùng
  `tests/fixtures/crawl_data/` (tổng hợp). `SEED_EMPLOYEE_PASSWORD` sống trong `infra/.env`.
  Stack thử riêng: `--project/--env-file` + biến `RETAIL_SYNC_*` cho `tests/scenarios/`.
- **`tests/scenarios/` là OPT-IN** (`make test-scenarios`, `RETAIL_SYNC_SCENARIOS=1`): chúng đổi
  trạng thái compose (AT-02 TẮT `central-api`). Đừng chạy khi đang có lần chạy bộ giả lập hay test
  ngâm trên compose. 3 cửa hàng: `--profile edge --profile edge-multi` (store-002/003, khóa
  `CENTRAL_API_KEY_STORE_00N` trong `infra/.env`).
- **Airflow 3 trên compose:** `api-server` (không còn `webserver`) + `dag-processor` + JWT chung
  (`AIRFLOW_JWT_SECRET`). dbt ở venv riêng, cosmos `InvocationMode.SUBPROCESS`. Bản dbt ghim ở
  `data_platform/requirements-dbt.txt` (một chỗ cho image, Makefile, test).

- **Metric (dashboard "sức khỏe luồng", [docs/17 §6](docs/17-data-flow.md), [10 §9](docs/10-observability.md)):**
  `store_id` phải là ATTRIBUTE của điểm đo — thuộc tính resource KHÔNG thành label trong Prometheus
  của `otel-lgtm`. Độ trễ request lấy từ FastAPI semconv ổn định (`setup_metrics()` bật), không tự
  tạo histogram giây (bucket mặc định 0…10000). `setup_metrics()` gọi TRƯỚC `instrument_fastapi()`.
  Outbox đo ở **edge-api**, không ở worker. Metric trạng thái S3–S6 do `pipeline monitor`
  (service `flow-monitor`) đọc. Thêm/đổi tên metric → sửa `flow-health.json` (test
  `test_flow_dashboard.py` đỏ nếu lệch). **Mật khẩu không bao giờ trong URL** (httpx log URL ở INFO):
  ClickHouse đăng nhập bằng header.

- **Việc có lịch ở trung tâm = `central-maintenance`** (partition `point_ledger` trước 3 tháng + đối
  soát INV-4, mỗi giờ; quét TOÀN BỘ mỗi 30 ngày lúc 01–05 giờ — lệnh `--full` từng không ai gọi).
  Hàm tạo partition từng không được ai gọi — thêm việc định kỳ mới thì gắn vào đây, đừng để nó chỉ
  nằm trong docstring. Cảnh báo sinh bằng `infra/observability/build_alerts.py` (21 rule, webhook →
  `alert-sink`), dashboard bằng `build_dashboard.py` — sửa script, đừng sửa tay JSON. Backup cửa hàng: sidecar `edge-backup-*`, kiểm bằng
  `infra/store_backup.py verify`. Log: `shared.logs.setup_logging()` (JSON + `trace_id`).

- **Giai đoạn B (2026-09-25), đừng làm hỏng:** (1) lô RỖNG `POST /events` là **heartbeat** — worker
  rảnh gửi mỗi 5 phút, trung tâm đặt `updated_at` và `lag_seconds = 0` (outbox trống = đã bắt kịp).
  (2) Rate limit theo cửa hàng `429` (token bucket, trong bộ nhớ một tiến trình) kiểm TRƯỚC semaphore
  `503`. Central API chạy N tiến trình (ADR-011): `--workers` và `CENTRAL_API_WORKERS` PHẢI cùng số;
  thứ gì theo tiến trình mà chạm Postgres thì chia qua `worker_budget()`, metric cần `service.instance.id`. (3) Trần backoff 240 s để CH-1 hội tụ < 5 phút. (4) dbt: fact đọc thẳng bronze, O(tháng)
  (quy tắc ở [data_platform/README.md](data_platform/README.md)). (5) Bộ giả lập: `5xx` ở chốt đơn =
  "không rõ kết cục". (6) `simulator bulk` ghi prefix `bronze/bulk-<profile>` + DB `dw_bulk`, KHÔNG BAO
  GIỜ `bronze/central`. (7) Test hỗn loạn/diễn tập/tải OPT-IN (`RETAIL_SYNC_CHAOS|ALERT_DRILLS|LOAD=1`),
  gây sự cố thật trên stack dev. `docker update --cpus 0` KHÔNG gỡ giới hạn CPU — đặt lại bằng số CPU
  của máy ảo. (8) Webhook Grafana không có uid rule trong nhãn — `alert-sink` lấy từ `generatorURL`.
  (9) Heartbeat là UPSERT `store_sync_status` — cửa hàng chưa từng bán phải hiện ra với `rs-store-silent`.
  (10) Mốc ngày có thể trước 1970 (`1970-01-01 - 7`) thì tính ở PYTHON và kẹp — `toDate32` KHÔNG đủ khi
  so với cột `Date` (ClickHouse ép hằng về `Date` → tràn thành 2149; bộ giám sát từng báo "0 đơn chờ dbt"
  trên stack sạch). (11) Test tích hợp KHÔNG dùng ngày cố định chạm partition `point_ledger` (chỉ giữ tháng
  trước + 3 tháng tới) — `date(2026, 8, 31)` đỏ từ 1/10. Lỗi kiểu này chỉ lộ trên máy SẠCH → workflow `proof`.

**Sync worker — quy tắc không được phá:** mất mạng/401/503 là lỗi của ĐƯỜNG TRUYỀN, không bao
giờ tăng `outbox.attempts`. Chỉ khi trung tâm xét và từ chối một sự kiện mới tính lượt thử.
Trộn hai loại thì cửa hàng offline vài phút tự vứt dữ liệu đúng vào dead-letter (AT-02).

**Bắt buộc đọc [docs/README.md](docs/README.md) trước khi đề xuất hay viết code.** Bộ docs
đó là nguồn sự thật cho thiết kế — requirements, quy mô, kiến trúc, tech stack, và các ADR
giải thích *vì sao*. Thư mục [docs/progress/](docs/progress/) là nhật ký tiến độ (đổi theo
từng tuần), KHÁC với bộ docs thiết kế `00`–`16` (không đổi theo tiến độ code).

## Trạng thái code hiện tại

| Thư mục | Trạng thái |
|---|---|
| `store/`, `central/` | **Prototype v1 — chỉ để tham khảo.** Không phát triển tiếp trên đó. Xem [docs/09-v1-postmortem.md](docs/09-v1-postmortem.md) |
| `crawl_data/` | **Tài sản giữ lại** — dữ liệu sản phẩm và cửa hàng thật, dùng làm master data cho v2 |
| `packages/` | **Source v2** — `shared` · `edge` (pos/loyalty/reporting/sync/web) · `central`. Tuần 1 xong: PlaceSale + Loyalty + auth JWT + UI POS + seed + OTel chạy thật. Tuần 2: **đồng bộ outbox → `POST /events` đã chạy thật** (2026-09-23, [nhật ký](docs/progress/2026-09-23-tuan-2-dong-bo.md)). Giai đoạn A: đường ghi cho bộ giả lập + đối soát INV-4 + trích xuất/nạp bronze + **dbt S6 + Airflow** + bộ giả lập `virtual` + `tests/scenarios/` xong — **cổng A đạt 2026-09-24**. Tiếp theo: giai đoạn B |
| `packages/simulator/` | **Bộ giả lập — nguồn dữ liệu của giai đoạn A/B** ([docs/18](docs/18-simulator.md)). Chế độ `edge` + **`virtual`** (tật `offline`/`resend`/`concurrent_customer`) + audit **L1…L4** chạy thật (2026-09-24). Là CLIENT: chỉ được import 4 module thuần của edge (cho chế độ `virtual`), không bao giờ import `central` (import-linter) |
| `packages/pipeline/` | **S4 trích xuất + S5 nạp bronze** ([docs/17](docs/17-data-flow.md)) — chạy thật trên compose (2026-09-24). **Bộ giám sát luồng** `monitor.py` (`python -m pipeline health|monitor`, 2026-09-25). Độc lập: không import `shared`/`edge`/`central`/`simulator` (chạy cả trong image Airflow). DDL bronze ở `ddl/bronze.sql` |
| `data_platform/` | **dbt S6** (staging → dim/fact, 32 test) + **DAG `retail_pipeline`** (Airflow 3.3.2 + cosmos), chạy thật trên compose (2026-09-24). Test: `tests/integration/test_dbt_marts.py` |
| `tests/`, `infra/` | Test, compose + Dockerfile |
| `docs/` | Thiết kế v2 — nguồn sự thật, đã hoàn tất |

Đừng sửa lỗi trong `store/` hay `central/` trừ khi được yêu cầu rõ ràng — v1 sẽ được thay
thế, không phải vá.

## 🎯 Ưu tiên số 1 (chủ dự án, 2026-09-11)

> **Hệ thống chạy ổn định, khả năng scale tốt** — đứng **trên** độ đầy đủ tính năng.

Hệ quả khi ra quyết định:
- Observability, test hỗn loạn, test ngâm, test tải là **Must**, không phải "nếu kịp"
- Tính năng phụ (trừ tồn kho, đối soát tiền chốt ca, SCD2, Metabase) hạ xuống **Could**.
  Từ 2026-09-23, **mọi** tính năng dùng tại quầy dời ra sau cổng B (ADR-010)
- Mọi scale seam phải được **chứng minh bằng số đo**, không chỉ ghi trong doc
- Quy tắc nghiệp vụ **cấu hình được**, không hardcode — chỉnh theo thực tế khi tích hợp
- Xem [docs/08-reliability-and-scale.md](docs/08-reliability-and-scale.md)

## Ràng buộc chi phối (đừng đề xuất thứ vi phạm chúng)

- **POC cho doanh nghiệp thật** — phải đúng nguyên lý, có đường lên production
- **1 người, ~1 tháng** — cắt phạm vi quyết liệt, không microservices
- **Docker trên laptop, ~16GB RAM** — tổng stack phải vừa một máy
- **Quy mô chưa xác định** — đơn giản trước, mọi lựa chọn phải có *scale seam* ghi trong ADR

## Bốn nguyên tắc kiến trúc

1. **Cửa hàng tự chủ** — phải bán được hàng khi mất mạng hoàn toàn. Ràng buộc cứng.
2. **Bất đồng bộ qua ranh giới mạng** — không giao dịch phân tán, không khóa xuyên mạng.
3. **Sự kiện bất biến, trạng thái suy ra được** — đặc biệt với điểm tích lũy.
4. **Idempotent ở mọi nơi** — mọi thao tác qua mạng đều sẽ bị lặp lại.

## Quyết định đã chốt (đừng đề xuất ngược lại mà không đọc ADR)

| Quyết định | ADR |
|---|---|
| PostgreSQL cho mọi OLTP — **không** MySQL, **không** Cassandra | [001](docs/adr/001-postgres-everywhere.md) |
| Điểm dùng **ledger append-only**, không bao giờ `UPDATE` số dư | [002](docs/adr/002-point-ledger.md) |
| Đồng bộ bằng **Outbox + HTTP**, Kafka để v2 | [003](docs/adr/003-outbox-not-kafka.md) |
| Edge là **modular monolith**, ranh giới cưỡng chế bằng `import-linter` | [004](docs/adr/004-modular-monolith.md) |
| **ClickHouse** warehouse + **MinIO/Parquet** lake + **dbt** | [005](docs/adr/005-clickhouse-warehouse.md) |
| **Airflow 3 + LocalExecutor** *(chủ dự án đã có kinh nghiệm)* | [007](docs/adr/007-airflow-over-dagster.md) — thay thế [006](docs/adr/006-dagster-over-airflow.md) |
| **HTMX + Alpine.js** cho UI, **không** redeem điểm v1, **không** multi-tenant | [008](docs/adr/008-remaining-decisions.md) |
| **OpenTelemetry + `grafana/otel-lgtm`** (compose profile riêng) để săn bottleneck | [009](docs/adr/009-observability-stack.md) |
| **Luồng dữ liệu trước, tính năng sau** — bộ giả lập thay UI, test tải bằng bộ giả lập (không `k6`), silver = staging dbt | [010](docs/adr/010-data-flow-first.md) |
| **Central API nhiều tiến trình** (`CENTRAL_API_WORKERS`, mặc định 4) — semaphore + pool chia theo tiến trình, rate limit không chia | [011](docs/adr/011-central-api-multi-process.md) |

✅ **Stack đã chốt 2026-09-11** — [docs/07-stack-decision.md](docs/07-stack-decision.md).

✅ **Giai đoạn thiết kế đã hoàn tất** (2026-09-12): hợp đồng sự kiện
([12](docs/12-event-schema.md)), API ([13](docs/13-api-contracts.md)), sơ đồ tuần tự
([14](docs/14-sequence-flows.md)), glossary ([15](docs/15-glossary.md)), bản đồ test
([16](docs/16-test-plan.md)), và `infra/compose.yaml` + `.env.example` đã có. Xem trạng thái từng hạng mục ở
[docs/11-design-readiness.md §6](docs/11-design-readiness.md).

Lý do cốt lõi đằng sau ADR-001 và 003: **tải ghi đỉnh ở 2000 cửa hàng chỉ ~67 writes/giây.**
Một PostgreSQL dư 25–100 lần. Bài toán này là bài toán *phân tán và độ tin cậy*, không phải
bài toán *throughput*. Xem [docs/02-scale-capacity.md](docs/02-scale-capacity.md) trước khi
đề xuất bất kỳ công nghệ phân tán nào.

## Tech stack v2

Python 3.12 · uv · FastAPI · Pydantic v2 · PostgreSQL 18 · Redis · SQLAlchemy 2 async +
Alembic · MinIO + Parquet · ClickHouse · dbt-core · **Airflow 3 (LocalExecutor) +
astronomer-cosmos** · **OpenTelemetry + grafana/otel-lgtm** · Metabase · pytest +
testcontainers · ruff + mypy

**Nguyên tắc chọn stack của chủ dự án:** *ưu tiên độ phù hợp cho hệ thống thực tế, không
chạy theo độ phổ biến.* Kinh nghiệm sẵn có được tính là ràng buộc kỹ thuật hợp lệ (ngân
sách 1 người/1 tháng), không phải "chạy theo đám đông".

## Mười ràng buộc thiết kế bắt buộc

Dễ quên, hỏng nặng nếu bỏ sót. Chi tiết + lý do ở
[docs/11-design-readiness.md §2](docs/11-design-readiness.md).

1. `synchronous_commit = on` ở Postgres cửa hàng — **không tắt để tối ưu** (không có UPS)
2. **Tự động tạo partition** `point_ledger` trước 3 tháng **và giữ tháng trước** — hết partition = toàn hệ thống điểm chết
3. Bronze phân vùng theo **`recorded_at`** (ngày nạp), không theo `occurred_at`
4. Idempotency ClickHouse dùng **`insert_deduplication_token`**, không dựa `ReplacingMergeTree`
5. **`fact_payment` tách riêng** — gộp vào `fact_sale_line` sẽ nhân doanh thu N×M lần
6. Xếp hạng dùng **`lifetime_earned`**, không phải `balance`
7. **`traceparent` trong payload outbox**, dùng **span link** (độ trễ có thể hàng giờ)
8. **Job đối soát tăng dần** ngay từ bản đầu — quét toàn bộ không khả thi ở T3
9. **Autovacuum riêng + partial index** cho `outbox` — bảng duy nhất sinh bloat
10. **Không PII** trong log, span, warehouse — chỉ `customer_id`

## Quy ước

- Ngôn ngữ tài liệu và giải thích: **tiếng Việt**. Tên định danh trong code: tiếng Anh.
- Tiền tệ: `bigint` đơn vị đồng. **Không bao giờ dùng float cho tiền.**
- Thời gian: `timestamptz` lưu UTC. Phân biệt `occurred_at` (tại cửa hàng) và `recorded_at`
  (tại trung tâm).
- ID giao dịch và sự kiện: **UUIDv7** sinh tại cửa hàng.
- `store_id` phải có trong **mọi** bảng giao dịch.
- Xếp hạng khách dựa trên `lifetime_earned`, **không** phải `balance` (tiêu điểm không được
  làm tụt hạng).
- **Bronze phân vùng theo `recorded_at` (ngày nạp), không theo `occurred_at`** — để dữ liệu
  đến trễ không phải viết lại lịch sử.
- Idempotency ClickHouse dùng **`insert_deduplication_token`**, không dựa vào
  `ReplacingMergeTree` (chỉ eventual, cần `FINAL`).
- Quy tắc nghiệp vụ (làm tròn, cộng gộp chiết khấu, số dư âm) là **config object**, không
  hardcode — nhưng `rounding` và `allow_negative_balance` có hệ quả `CHECK` constraint.
- Thay đổi quyết định lớn → viết ADR mới, đánh dấu ADR cũ là `Superseded`. Không xóa ADR.
- **Code lệch docs = bug ở một trong hai.** Nếu hiện thực buộc phải lệch (Postgres không cho
  phép, chẳng hạn), sửa docs kèm changelog — đừng để hai bên nói khác nhau.
- Không viết stub/route rỗng để "giữ chỗ": hợp đồng đã nằm ở
  [docs/13](docs/13-api-contracts.md), lặp lại thành code chết chỉ gây nhầm đã xong.
- **File `.ini` phải THUẦN ASCII** (`alembic.ini`, `.importlinter`). `ConfigParser` đọc
  chúng bằng encoding *locale* — cp1252 trên Windows mặc định — nên một ký tự tiếng Việt
  làm cả `alembic upgrade` lẫn `lint-imports` chết bằng `UnicodeDecodeError` trước khi chạy
  được gì. Giải thích tiếng Việt đặt trong docstring Python (luôn UTF-8), không đặt trong
  `.ini`. TOML/YAML/Markdown thì không sao — chúng bắt buộc UTF-8 theo đặc tả.

## Môi trường

Windows 11, PowerShell là shell chính (Bash cũng có). **Không mount volume database vào
OneDrive** — rủi ro hỏng dữ liệu. Dùng named volume thay vì bind mount cho DB.

- **PostgreSQL 18 đổi quy ước mount volume**: named volume phải trỏ THẲNG vào
  `/var/lib/postgresql`, không còn `.../data` như PG < 18 — mount sai chỗ thì container từ
  chối khởi động (xem comment đầu `infra/compose.yaml`).
- **`testcontainers`/Ryuk có race lúc khởi động** trên máy này (cổng NAT chưa sẵn sàng lúc
  healthcheck đầu, `ConnectionError: Port mapping ... is not available`). Chạy test với
  `TESTCONTAINERS_RYUK_DISABLED=true` (đã đặt sẵn trong `make test-all` và CI) — không lùi
  về vòng lặp retry-theo-may-rủi.
