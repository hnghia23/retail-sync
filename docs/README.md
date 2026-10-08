# retail-sync — Tài liệu thiết kế

> **Trạng thái:** Thiết kế hoàn tất. Code v2 trong `packages/`. **🚪 Cổng A (luồng dữ liệu
> thông suốt) đạt 2026-09-24** — đang sang **giai đoạn B** (chứng minh ổn định & scale).
> **Cập nhật:** 2026-09-25
>
> 🔀 **Hướng phát triển hiện tại — [ADR-010](adr/010-data-flow-first.md):** chỉ tập trung vào
> **luồng dữ liệu**, không dựng tiếp ứng dụng để dùng. Dữ liệu vào bằng **bộ giả lập**, không
> qua UI. Thứ tự: **A** luồng dữ liệu thông suốt → **B** chứng minh ổn định & scale → **C** tính
> năng. Người mới đọc [17-data-flow](17-data-flow.md) và [18-simulator](18-simulator.md) ngay
> sau 00–03.

Đây là bộ tài liệu thiết kế cho bản **làm lại từ đầu** của retail-sync. Source v2 nằm ở
`packages/`. Code trong `store/`, `central/` là **prototype v1 — chỉ dùng để tham khảo**,
không phải nền tảng để phát triển tiếp. Xem [09-v1-postmortem.md](09-v1-postmortem.md) để biết v1 sai ở đâu và
tại sao lại làm lại.

## Đọc theo thứ tự

| # | Tài liệu | Trả lời câu hỏi |
|---|---|---|
| 00 | [00-context.md](00-context.md) | Dự án này là gì, cho ai, ràng buộc nào? |
| 01 | [01-requirements.md](01-requirements.md) | Hệ thống phải làm được gì? (FR + NFR) |
| 02 | [02-scale-capacity.md](02-scale-capacity.md) | Quy mô bao nhiêu? Tải thật là bao nhiêu? |
| 03 | [03-architecture.md](03-architecture.md) | Kiến trúc nào? Ranh giới ở đâu? |
| 04 | [04-tech-stack.md](04-tech-stack.md) | Dùng công nghệ gì, vì sao, và bỏ gì? |
| 05 | [05-data-model.md](05-data-model.md) | Dữ liệu trông như thế nào? |
| 06 | [06-roadmap.md](06-roadmap.md) | Làm gì, theo thứ tự nào? *(giai đoạn A → B → C)* |
| **07** | **[07-stack-decision.md](07-stack-decision.md)** | **✅ Bảng chốt stack — lý do từng lựa chọn** |
| **08** | **[08-reliability-and-scale.md](08-reliability-and-scale.md)** | **Chế độ hỏng, backpressure, observability, kế hoạch chứng minh ổn định & scale** |
| 09 | [09-v1-postmortem.md](09-v1-postmortem.md) | Prototype cũ sai ở đâu? |
| **10** | **[10-observability.md](10-observability.md)** | **Săn bottleneck: OpenTelemetry, trace qua outbox, profiling DB** |
| **11** | **[11-design-readiness.md](11-design-readiness.md)** | **⭐ Điểm vào giai đoạn thiết kế: 10 ràng buộc bắt buộc, cần sản xuất ra gì** |
| 12 | [12-event-schema.md](12-event-schema.md) | Hợp đồng sự kiện outbox — envelope, versioning, idempotency |
| 13 | [13-api-contracts.md](13-api-contracts.md) | Route Edge API + Central API, hợp đồng lỗi |
| 14 | [14-sequence-flows.md](14-sequence-flows.md) | 4 luồng còn thiếu: trả hàng, kéo master data, chốt ca, đồng bộ lỗi |
| 15 | [15-glossary.md](15-glossary.md) | Thuật ngữ dùng thống nhất |
| 16 | [16-test-plan.md](16-test-plan.md) | Bản đồ AT/CH/LD/DI → file test cụ thể |
| **17** | **[17-data-flow.md](17-data-flow.md)** | **⭐ Luồng dữ liệu đầu-cuối: 6 chặng, 5 bẫy phải xử lý, đối soát xuyên tầng, "ổn định" đo thế nào** |
| **18** | **[18-simulator.md](18-simulator.md)** | **⭐ Bộ giả lập: 3 chế độ, mô hình sinh dữ liệu, manifest đáp án** |
| — | [diagrams/system-overview.html](diagrams/system-overview.html) | Sơ đồ trực quan: kiến trúc + tech stack + 8 chú thích ADR — mở bằng trình duyệt |
| 99 | [99-original-spec.md](99-original-spec.md) | Spec gốc nguyên văn (tham chiếu) |

### 📁 [business/](business/) — Nghiệp vụ & vận hành thực tế

Dẫn xuất yêu cầu từ **hành vi thật** thay vì từ kiến trúc trên giấy. Đọc khi muốn biết hệ
thống sẽ được dùng như thế nào, và khi muốn kiểm tra lại một quyết định kỹ thuật.

| # | Tài liệu | Trả lời |
|---|---|---|
| B01 | [business/01-operating-model.md](business/01-operating-model.md) | Ai làm gì, nhịp ngày/tuần, thực tế hạ tầng cửa hàng |
| B02 | [business/02-edge-cases.md](business/02-edge-cases.md) | ~50 tình huống thực tế — cái nào chưa xử lý |
| B03 | [business/03-analytical-workload.md](business/03-analytical-workload.md) | Truy vấn nào thật sự chạy, engine nào đúng |
| B04 | [business/04-stack-implications.md](business/04-stack-implications.md) | **Stack phải thay đổi gì** |

## Architecture Decision Records

Mỗi quyết định lớn kèm bối cảnh và phương án đã loại. Đọc khi muốn biết **vì sao**, hoặc
khi muốn đảo ngược một quyết định.

| ADR | Quyết định |
|---|---|
| [001](adr/001-postgres-everywhere.md) | PostgreSQL cho mọi OLTP (thay MySQL + Cassandra) |
| [002](adr/002-point-ledger.md) | Điểm tích lũy dùng ledger append-only, không UPDATE số dư |
| [003](adr/003-outbox-not-kafka.md) | Đồng bộ bằng Outbox + HTTP ở v1, Kafka để sau |
| [004](adr/004-modular-monolith.md) | Edge là modular monolith, không phải microservices |
| [005](adr/005-clickhouse-warehouse.md) | ClickHouse làm warehouse, MinIO+Parquet làm lake |
| [006](adr/006-dagster-over-airflow.md) | ~~Dagster thay Airflow~~ — ❌ bị thay bởi ADR-007 |
| [007](adr/007-airflow-over-dagster.md) | **Airflow 3 + LocalExecutor** (quyết định của chủ dự án) |
| [008](adr/008-remaining-decisions.md) | **Chốt các quyết định còn lại** — DB cửa hàng, UI, dbt, redeem, multi-tenant |
| [009](adr/009-observability-stack.md) | **OpenTelemetry + `grafana/otel-lgtm`** để săn bottleneck |
| [010](adr/010-data-flow-first.md) | **Luồng dữ liệu trước, tính năng sau** — bộ giả lập thay UI, giai đoạn A/B/C |
| [011](adr/011-central-api-multi-process.md) | **Central API nhiều tiến trình** — gỡ trần một nhân (LD-2); ngân sách Postgres chia theo tiến trình |

> **✅ Stack đã chốt (2026-09-11)** — [07-stack-decision.md](07-stack-decision.md).
>
> 🎯 **Ưu tiên số 1: ổn định + scale** — xem [08-reliability-and-scale.md](08-reliability-and-scale.md).
> Verification là Must; tính năng phụ hạ xuống Could.
>
> ✅ **Toàn bộ docs đã đồng bộ (2026-09-11)** — data model đã gộp 8 lỗ hổng schema, kiến trúc
> đã có lớp báo cáo vận hành, lộ trình đã gộp +2 ngày Must.
>
> ✅ **Đã bổ sung 12–16 + `infra/compose.yaml` + `.env.example`** — hợp đồng sự kiện, API,
> 4 sơ đồ tuần tự còn thiếu, glossary, bản đồ test. Xem tình trạng cập nhật ở
> [11-design-readiness.md §8](11-design-readiness.md).
>
> ✅ **Đã viết (2026-09-17):** khung source (`packages/`), migration Alembic cho cả hai DB
> — đã chạy trên PostgreSQL 18.6 và có test bất biến, hàm tự tạo partition `point_ledger`,
> Dockerfile, CI. Xem [11-design-readiness.md §6](11-design-readiness.md).
>
> ✅ **Tuần 1 đóng cổng (2026-09-18); đồng bộ cửa hàng → trung tâm chạy thật (2026-09-23).**
>
> ✅ **Giai đoạn A xong, cổng A đạt (2026-09-24)** — luồng S1 → S6 chạy hết trên compose: cửa
> hàng → outbox → trung tâm → lake (Parquet/MinIO) → bronze (ClickHouse) → dbt → mart, lên lịch
> bằng Airflow 3. Bộ giả lập (`edge` 3 cửa hàng thật, `virtual` 20 cửa hàng ảo có tật) + bộ đối
> soát 5 tầng L0…L4 `CONVERGED`; `tests/scenarios/` AT-01…04, 07, 10. Nhật ký:
> [progress/2026-09-24-cong-a.md](progress/2026-09-24-cong-a.md) và các file cùng ngày.
>
> ✅ **Giai đoạn B, việc 1: dashboard "sức khỏe luồng"** (2026-09-25) — metric OTel theo chặng
> S1 → S6, bộ giám sát `pipeline monitor`, audit báo độ tươi, Grafana `http://localhost:3001`
> ([17 §6](17-data-flow.md), [progress](progress/2026-09-25-giai-doan-b-dashboard.md)).
>
> **Việc tiếp theo — giai đoạn B** ([06](06-roadmap.md)): job CI cho `tests/scenarios/` → test
> hỗn loạn CH-1…7 → test tải LD-1…4 (bộ giả lập `virtual`, `bulk`) → test ngâm 72h.

## Nguyên tắc xuyên suốt

1. **Đơn giản trước, có đường mở rộng rõ ràng** — không dùng công nghệ phân tán khi một
   node còn dư 25 lần. Nhưng mọi lựa chọn phải có "scale seam" ghi rõ trong ADR.
2. **Cửa hàng phải bán được hàng khi mất mạng.** Đây là ràng buộc cứng, định hình mọi
   quyết định về đồng bộ.
3. **Điểm tích lũy là dữ liệu tài chính** — phải kiểm toán được, không được mất, không
   được cộng hai lần.
4. **Một người phải vận hành được.** Mỗi thành phần thêm vào phải trả giá bằng thời gian
   vận hành.

## Quy ước khi cập nhật docs

- Thay đổi quyết định → **sửa ADR cũ thành `Superseded by ADR-XXX`**, viết ADR mới. Không
  xóa ADR.
- Thay đổi requirement → sửa trực tiếp `01-requirements.md`, ghi ngày vào changelog cuối file.
- Docs là nguồn sự thật cho thiết kế. Code lệch docs = bug ở một trong hai.
