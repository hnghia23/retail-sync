# retail-sync

Hệ thống dữ liệu cho chuỗi cửa hàng bán lẻ: **POS** (đơn hàng + khách hàng) và **Loyalty**
(tích điểm), kèm tầng phân tích trung tâm.

> ⚠️ **Đang viết code v2** trong `packages/` — thiết kế đã hoàn tất, **tuần 1 đã đóng
> cổng** (2026-09-18), đang ở đầu tuần 2. Code trong `store/` và `central/` là prototype
> v1 — chỉ để tham khảo, không phát triển tiếp trên đó.
> **Đọc [docs/README.md](docs/README.md) trước khi làm bất cứ việc gì**, và
> [docs/progress/](docs/progress/) để biết đã làm tới đâu mà không cần đọc lại toàn repo.

## Bắt đầu từ đâu

| Bạn là ai | Đọc gì trước |
|---|---|
| Muốn hiểu dự án làm gì, cho ai | [docs/00-context.md](docs/00-context.md) |
| Muốn xem sơ đồ tổng quan trực quan | [docs/diagrams/system-overview.html](docs/diagrams/system-overview.html) *(mở bằng trình duyệt)* |
| Muốn biết stack dùng gì, vì sao | [docs/07-stack-decision.md](docs/07-stack-decision.md) |
| Sắp viết code | [docs/11-design-readiness.md](docs/11-design-readiness.md) |
| Muốn biết đã làm tới đâu (không đọc lại toàn repo) | [docs/progress/](docs/progress/) — mới nhất: [2026-09-18-tong-ket-tuan-1.md](docs/progress/2026-09-18-tong-ket-tuan-1.md) |
| Claude Code / AI agent | [CLAUDE.md](CLAUDE.md) |

## Trạng thái

| Thư mục | Trạng thái |
|---|---|
| `docs/` | Thiết kế v2 — **21 tài liệu + 9 ADR**, giai đoạn phân tích đã hoàn tất |
| `docs/progress/` | Nhật ký tiến độ theo tuần — tuần 1 đã đóng cổng (2026-09-18) |
| `store/`, `central/` | Prototype v1 — tham khảo, không phát triển tiếp. Xem [docs/09-v1-postmortem.md](docs/09-v1-postmortem.md) |
| `crawl_data/` | Dữ liệu sản phẩm/cửa hàng thật — đã seed vào v2 (5556 sản phẩm, 3515 cửa hàng) |
| `packages/` | **Source v2** — tuần 1 xong: PlaceSale, Loyalty, auth JWT, UI POS, OTel. Tuần 2 đang viết: sync worker, đồng bộ, trả hàng/ca/báo cáo |
| `data_platform/` | Airflow DAG + dbt (lớp C, T+1) — chưa có nội dung, thuộc tuần 3 |
| `infra/` | Compose 5 profile chạy được thật (`edge`/`central`/`data`/`bi`/`observability`) |
| `tests/` | unit · integration (testcontainers) · scenarios — 220/220 pass, bản đồ ở [docs/16](docs/16-test-plan.md) |

## Ưu tiên số 1

> **Hệ thống chạy ổn định, khả năng scale tốt** — đứng trên độ đầy đủ tính năng.

Xem [docs/08-reliability-and-scale.md](docs/08-reliability-and-scale.md).

## Chạy thử (spike / phát triển)

Cần [uv](https://docs.astral.sh/uv/) và Docker Desktop.

```bash
cp infra/.env.example infra/.env   # điền giá trị thật — KHÔNG commit
uv sync --all-extras --dev         # cài dependency (Python 3.12)
make lint test                     # ruff + mypy + ranh giới module + unit test
make up                            # docker compose --profile edge --profile central
```

`make help` liệt kê đầy đủ lệnh. Không có `make` trên Windows? Mở
[Makefile](Makefile) và gõ thẳng lệnh `uv run ...` tương ứng.

Xem đầy đủ các profile (`edge`, `central`, `data`, `bi`, `observability`) trong
[infra/compose.yaml](infra/compose.yaml) và [docs/06-roadmap.md](docs/06-roadmap.md) (mục
"Ngày 0 — Spike xác minh").

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
└── central/          ingest (idempotency) · lookup · reporting · ops (seed, bảo trì partition) · migrations

data_platform/        airflow/dags · dbt · seeds
simulator/            sinh tải, mô phỏng nhiều cửa hàng (LD-1..4, CH-6)
infra/docker/         app.Dockerfile (chung cho 3 service) + airflow.Dockerfile
tests/                unit · integration (testcontainers) · scenarios · load
```

Ranh giới module được cưỡng chế bằng `import-linter` ([.importlinter](.importlinter)) trong
CI — không dựa vào kỷ luật con người (ADR-004).

API Edge đã có `/health`, `/api/v1/auth/login`, `/api/v1/sales`, `/ui/*` (UI POS HTMX) chạy
thật; Central API có `/health`. Route còn thiếu (`POST /events`, `/returns`, `/shifts`,
`/reports`) thuộc tuần 2 — xem [docs/progress/](docs/progress/) cho danh sách đầy đủ. Hợp
đồng đường dẫn, vai trò và mã lỗi nằm ở [docs/13-api-contracts.md](docs/13-api-contracts.md).

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
├── adr/                    9 Architecture Decision Record
├── business/                phân tích nghiệp vụ & vận hành thực tế
└── diagrams/                sơ đồ trực quan (HTML, mở bằng trình duyệt)
```

---
*README này thay thế bản mô tả kiến trúc v1 cũ (MySQL/Cassandra/Kafka/Airflow LocalExecutor
4 container) — đã lỗi thời so với quyết định trong `docs/`. Xem
[docs/09-v1-postmortem.md](docs/09-v1-postmortem.md) nếu cần đối chiếu.*
