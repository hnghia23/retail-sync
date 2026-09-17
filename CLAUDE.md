# retail-sync

Hệ thống dữ liệu cho chuỗi cửa hàng bán lẻ: POS (đơn hàng + khách hàng) và Loyalty (tích điểm),
kèm tầng phân tích trung tâm.

## ⚠️ Đọc trước khi làm bất cứ việc gì

Thiết kế v2 **đã hoàn tất**; đang **viết code v2** trong `packages/`.
**Tuần 1 ([lộ trình](docs/06-roadmap.md)) đã đóng cổng (2026-09-18)** — xem tổng kết ở
[docs/progress/2026-09-18-tong-ket-tuan-1.md](docs/progress/2026-09-18-tong-ket-tuan-1.md)
(đã làm gì, đã kiểm chứng ra sao, demo được gì, còn thiếu gì). Đang ở **đầu tuần 2**
(Loyalty + Ledger + Đồng bộ + Báo cáo vận hành).

**Bắt buộc đọc [docs/README.md](docs/README.md) trước khi đề xuất hay viết code.** Bộ docs
đó là nguồn sự thật cho thiết kế — requirements, quy mô, kiến trúc, tech stack, và các ADR
giải thích *vì sao*. Thư mục [docs/progress/](docs/progress/) là nhật ký tiến độ (đổi theo
từng tuần), KHÁC với bộ docs thiết kế `00`–`16` (không đổi theo tiến độ code).

## Trạng thái code hiện tại

| Thư mục | Trạng thái |
|---|---|
| `store/`, `central/` | **Prototype v1 — chỉ để tham khảo.** Không phát triển tiếp trên đó. Xem [docs/09-v1-postmortem.md](docs/09-v1-postmortem.md) |
| `crawl_data/` | **Tài sản giữ lại** — dữ liệu sản phẩm và cửa hàng thật, dùng làm master data cho v2 |
| `packages/` | **Source v2** — `shared` · `edge` (pos/loyalty/reporting/sync/web) · `central`. Tuần 1 xong: PlaceSale + Loyalty + auth JWT + UI POS + seed + OTel chạy thật. Tuần 2 đang viết: sync worker, `POST /events`, tra cứu khách hàng, trả hàng/ca/báo cáo |
| `tests/`, `infra/`, `data_platform/`, `simulator/` | Test, compose + Dockerfile, Airflow/dbt, sinh tải |
| `docs/` | Thiết kế v2 — nguồn sự thật, đã hoàn tất |

Đừng sửa lỗi trong `store/` hay `central/` trừ khi được yêu cầu rõ ràng — v1 sẽ được thay
thế, không phải vá.

## 🎯 Ưu tiên số 1 (chủ dự án, 2026-09-11)

> **Hệ thống chạy ổn định, khả năng scale tốt** — đứng **trên** độ đầy đủ tính năng.

Hệ quả khi ra quyết định:
- Observability, test hỗn loạn, test ngâm, test tải là **Must**, không phải "nếu kịp"
- Tính năng phụ (trừ tồn kho, đối soát tiền chốt ca, SCD2, Metabase) hạ xuống **Could**
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
2. **Tự động tạo partition** `point_ledger` trước 3 tháng — hết partition = toàn hệ thống điểm chết
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
