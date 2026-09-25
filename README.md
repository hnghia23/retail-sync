# retail-sync

Hệ thống dữ liệu cho chuỗi cửa hàng bán lẻ: **POS** (đơn hàng + khách hàng) và **Loyalty**
(tích điểm), kèm tầng phân tích trung tâm.

> ⚠️ **Đang viết code v2** trong `packages/` — thiết kế đã hoàn tất, tuần 1 đóng cổng
> (2026-09-18), **🚪 cổng A đạt (2026-09-24)**: luồng dữ liệu cửa hàng → trung tâm → lake →
> ClickHouse → dbt chạy hết trên compose, đối soát 5 tầng khớp. Đang sang **giai đoạn B**. Code trong
> `store/` và `central/` là prototype v1 — chỉ để tham khảo, không phát triển tiếp trên đó.
>
> 🔀 **Hướng hiện tại ([ADR-010](docs/adr/010-data-flow-first.md)):** chỉ phát triển **luồng
> dữ liệu** cửa hàng → trung tâm → lake → warehouse, dùng **dữ liệu giả lập** thay UI, chứng
> minh ổn định bằng hỗn loạn/ngâm/tải. Tính năng dùng tại quầy làm sau.
>
> **Đọc [docs/README.md](docs/README.md) trước khi làm bất cứ việc gì**, và
> [docs/progress/](docs/progress/) để biết đã làm tới đâu mà không cần đọc lại toàn repo.

## Bắt đầu từ đâu

| Bạn là ai | Đọc gì trước |
|---|---|
| Muốn hiểu dự án làm gì, cho ai | [docs/00-context.md](docs/00-context.md) |
| Muốn xem sơ đồ tổng quan trực quan | [docs/diagrams/system-overview.html](docs/diagrams/system-overview.html) *(mở bằng trình duyệt)* |
| Muốn biết stack dùng gì, vì sao | [docs/07-stack-decision.md](docs/07-stack-decision.md) |
| Sắp viết code | [docs/17-data-flow.md](docs/17-data-flow.md) (luồng + 5 bẫy) · [docs/18-simulator.md](docs/18-simulator.md) · [docs/11-design-readiness.md](docs/11-design-readiness.md) |
| Muốn biết đã làm tới đâu (không đọc lại toàn repo) | [docs/progress/](docs/progress/) — mới nhất: [2026-09-24-cong-a.md](docs/progress/2026-09-24-cong-a.md) (🚪 cổng A đạt) |
| Claude Code / AI agent | [CLAUDE.md](CLAUDE.md) |

## Trạng thái

| Thư mục | Trạng thái |
|---|---|
| `docs/` | Thiết kế v2 — **20 tài liệu + 4 tài liệu nghiệp vụ + 10 ADR**, giai đoạn phân tích đã hoàn tất |
| `docs/progress/` | Nhật ký tiến độ — tuần 1 đóng cổng (2026-09-18), đồng bộ xong + đổi hướng (2026-09-23), luồng S1→S6 chạy hết + **cổng A đạt** (2026-09-24) |
| `store/`, `central/` | Prototype v1 — tham khảo, không phát triển tiếp. Xem [docs/09-v1-postmortem.md](docs/09-v1-postmortem.md) |
| `crawl_data/` | Dữ liệu sản phẩm/cửa hàng thật — đã seed vào v2 (5556 sản phẩm, 3515 cửa hàng) |
| `packages/` | **Source v2** — `shared` · `edge` · `central`: PlaceSale, Loyalty, auth JWT, UI POS (đóng băng), OTel, đồng bộ outbox → `POST /events`, đường ghi `/customers` + `/shifts/*` cho bộ giả lập, đối soát INV-4 |
| `packages/pipeline/` | S4 trích xuất Postgres trung tâm → Parquet (MinIO) + S5 nạp bronze ClickHouse — độc lập, chạy cả trong image Airflow |
| `packages/simulator/` | Bộ giả lập — nguồn dữ liệu của giai đoạn A/B ([docs/18](docs/18-simulator.md)): chế độ `edge` (cửa hàng thật) + `virtual` (cửa hàng ảo có tật), manifest, audit L0…L4 |
| `data_platform/` | dbt S6 (staging → dim/fact) + DAG Airflow 3 `retail_pipeline` nối S4 → S5 → S6 ([docs/17](docs/17-data-flow.md)); trích xuất/nạp ở `packages/pipeline/` |
| `infra/` | Compose 6 profile chạy được thật (`edge`/`edge-multi`/`central`/`data`/`bi`/`observability`) — `edge-multi` thêm store-002/003 |
| `tests/` | unit · integration (testcontainers) · scenarios — 396 pass + 7 kịch bản compose (opt-in), bản đồ ở [docs/16](docs/16-test-plan.md) |

## Ưu tiên số 1

> **Hệ thống chạy ổn định, khả năng scale tốt** — đứng trên độ đầy đủ tính năng.

Xem [docs/08-reliability-and-scale.md](docs/08-reliability-and-scale.md).

## Chạy thử (spike / phát triển)

Cần [uv](https://docs.astral.sh/uv/) và Docker Desktop.

```bash
uv sync --all-extras --dev                            # cài dependency (Python 3.12)
make lint test                                        # ruff + mypy + ranh giới module + unit test
uv run python infra/bootstrap.py --observability      # = make bootstrap OBS=1
```

`infra/bootstrap.py` dựng cả stack từ con số 0: tạo `infra/.env` từ `.env.example` nếu chưa có,
migrate 4 Postgres, seed master data, cấp khóa sync cho 3 cửa hàng + 20 cửa hàng ảo (giữ khóa
cũ nếu trung tâm còn nhận), nhân viên dựng sẵn (mật khẩu sinh vào `infra/.env`), lake + bảng
bronze, rồi chờ Airflow đọc DAG. Chạy lại bao nhiêu lần cũng được. CI chạy đúng lệnh này.

Dữ liệu master: `crawl_data/` (dữ liệu crawl thật) **không nằm trong git**. Không có nó thì
bootstrap dùng bộ tổng hợp `tests/fixtures/crawl_data/` cùng định dạng (250 sản phẩm, 32 cửa
hàng) — đủ cho bộ giả lập và `tests/scenarios/`.

Dashboard "Sức khỏe luồng dữ liệu": http://localhost:3001 (Grafana, `admin`/`admin`), thư mục
`retail-sync` ([docs/17 §6](docs/17-data-flow.md)).

`make help` liệt kê đầy đủ lệnh. Không có `make` trên Windows? Mở
[Makefile](Makefile) và gõ thẳng lệnh `uv run ...` tương ứng.

Xem đầy đủ các profile (`edge`, `edge-multi`, `central`, `data`, `bi`, `observability`) trong
[infra/compose.yaml](infra/compose.yaml) và [docs/06-roadmap.md](docs/06-roadmap.md) (mục
"Ngày 0 — Spike xác minh").

Luồng dữ liệu đầu-cuối (giai đoạn A, [docs/17](docs/17-data-flow.md)):

```bash
make sim-run                       # bộ giả lập chế độ edge → Edge API thật → outbox → trung tâm
make dag-run                       # S4 trích xuất → S5 nạp bronze → S6 dbt (cũng chạy theo lịch)
make sim-audit MANIFEST=runs/<id>/manifest.json EDGE_DSN=... CENTRAL_DSN=... CLICKHOUSE=...
make provision-virtual && make sim-virtual   # 20 cửa hàng ảo có tật offline/resend/...
make test-scenarios                # AT-01…04, 07, 10 trên compose thật (tắt central-api ở AT-02!) — CI chạy mỗi PR
```

## Bản đồ source

```
packages/
├── shared/           hợp đồng sự kiện (docs/12), config nghiệp vụ, outbox, tracing, db
├── edge/             ứng dụng cửa hàng — modular monolith (ADR-004)
│   ├── pos/            domain · application/ports.py · adapters   ← ranh giới nghiêm ngặt
│   ├── loyalty/        domain · application · adapters · api.py (cửa duy nhất pos đi qua)
│   ├── reporting/      lớp truy vấn A — đọc thẳng PG cửa hàng, chạy được khi offline
│   ├── web/             UI POS — HTMX + Alpine vendor cục bộ (không CDN), gọi use case thật
│   ├── sync/            outbox worker (tiến trình riêng)
│   ├── ops/             seed dữ liệu từ crawl_data/
│   └── migrations/      Alembic — schema cửa hàng
├── central/          ingest (idempotency) · ops (seed, partition, đối soát INV-4, cấp khóa) · migrations
├── simulator/        bộ giả lập: nguồn dữ liệu thay UI (edge ✅ / virtual ✅ / bulk — B) + tật + manifest + audit L0…L4
└── pipeline/         S4 trích xuất → bronze Parquet (MinIO) · S5 nạp ClickHouse · ddl/bronze.sql

data_platform/        airflow/dags/retail_pipeline.py · dbt (staging → dim/fact, 32 test) · requirements(-dbt).txt
infra/docker/         app.Dockerfile (chung cho edge/central) + airflow.Dockerfile
tests/                unit · integration (testcontainers) · scenarios (compose thật, opt-in)
```

Ranh giới module được cưỡng chế bằng `import-linter` ([.importlinter](.importlinter)) trong
CI — không dựa vào kỷ luật con người (ADR-004).

API Edge đã có `/health`, `/api/v1/auth/login`, `/api/v1/sales`, `/api/v1/sales/quote`, `/api/v1/customers`,
`/api/v1/shifts/open`, `/api/v1/shifts/{id}/close`, `/ui/*` (UI POS HTMX, đóng băng) chạy thật; Central API có `/health` và `POST /api/v1/events` (ingest, xác thực bằng khóa cửa hàng).
Route nào làm ở giai đoạn nào: bảng đầu [docs/13-api-contracts.md](docs/13-api-contracts.md),
nơi cũng ghi hợp đồng đường dẫn, vai trò và mã lỗi.

## Bản đồ tài liệu

```
docs/
├── README.md              ← mục lục đầy đủ, đọc từ đây
├── 00-context ... 11-design-readiness    (yêu cầu, kiến trúc, stack, lộ trình)
├── 12-event-schema         hợp đồng sự kiện outbox
├── 13-api-contracts        route Edge API / Central API
├── 14-sequence-flows       4 luồng còn lại (trả hàng, kéo master data, chốt ca, đồng bộ lỗi)
├── 15-glossary             thuật ngữ dùng thống nhất
├── 16-test-plan            bản đồ AT/CH/LD/DI → file test
├── 17-data-flow            ⭐ luồng dữ liệu đầu-cuối: 6 chặng, 5 bẫy, đối soát xuyên tầng
├── 18-simulator            ⭐ bộ giả lập: 3 chế độ, mô hình sinh dữ liệu, manifest
├── progress/               nhật ký tiến độ theo ngày (khác với docs thiết kế)
├── adr/                    10 Architecture Decision Record
├── business/                phân tích nghiệp vụ & vận hành thực tế
└── diagrams/                sơ đồ trực quan (HTML, mở bằng trình duyệt)
```

---
*README này thay thế bản mô tả kiến trúc v1 cũ (MySQL/Cassandra/Kafka/Airflow LocalExecutor
4 container) — đã lỗi thời so với quyết định trong `docs/`. Xem
[docs/09-v1-postmortem.md](docs/09-v1-postmortem.md) nếu cần đối chiếu.*
