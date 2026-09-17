# Tổng kết tiến độ — chốt tuần 1 (2026-09-18)

> Nhật ký tiến độ, khác với bộ docs thiết kế `00`–`16` (nguồn sự thật thiết kế, không đổi
> theo tiến độ code). File này ghi lại **đã làm gì, kiểm chứng ra sao, còn thiếu gì** — đọc
> trước khi tiếp tục tuần 2 để không làm lại việc đã xong hoặc quên việc đang dang dở.

## 1. Phạm vi đã hoàn thành

### Hạ tầng & khung dự án
- Modular monolith `packages/edge` (pos · loyalty · reporting · sync · web · ops) + `packages/central`,
  ranh giới cưỡng chế bằng `import-linter` (8 hợp đồng, [ADR-004](../adr/004-modular-monolith.md))
- Alembic migration THẬT cho cả edge và central, chạy và xác minh trên PostgreSQL 18 thật
  (không phải SQL sinh mẫu) — 15 bảng phía edge, đủ partition tự động cho `point_ledger`
- `infra/compose.yaml` — 5 profile (`edge`, `central`, `data`, `bi`, `observability`), đã
  vá lỗi thật lúc dựng: PG18 đổi quy ước mount volume (`/var/lib/postgresql`, không còn
  `.../data`)

### Nghiệp vụ lõi
- Vertical slice **PlaceSale** đầy đủ: domain (`pricing.py`) → use case
  (`place_sale.py`) → Postgres adapters → route `POST /api/v1/sales` **và**
  `POST /ui/pos/checkout` — CÙNG một use case, không có logic riêng cho UI
- Loyalty: tích điểm + xếp hạng theo `lifetime_earned` (không phải `balance`, theo quy ước
  bắt buộc), ledger append-only ([ADR-002](../adr/002-point-ledger.md))
- Ba bất biến kế toán (INV-1/2/3) cưỡng chế ở cả use case lẫn DB trigger, kiểm bằng SQL
  thật trên Postgres

### Bảo mật
- JWT (access 15p không mang role tồn lâu / refresh 7 ngày không mang role) + Argon2,
  `shared/security.py` là nguồn giải mã token DUY NHẤT cho cả API JSON (Bearer) và UI
  (cookie httponly)
- Lỗ hổng thật tìm và vá: `POST /sales` từng tin `employee_id` client gửi lên — giờ chống
  giả mạo bằng cách đối chiếu với claims trong JWT

### Dữ liệu thật
- Seed `product_cache` (5556 sản phẩm) và `store` trung tâm (3515 cửa hàng WinMart) từ
  `crawl_data/` thật, xử lý các lỗi chất lượng dữ liệu thật gặp trong CSV gốc (mã trùng,
  barcode trùng)

### Quan sát hệ thống
- OpenTelemetry + `grafana/otel-lgtm`, kiểm chứng live: trace `edge-api` và `central-api`
  hiện trong Tempo, có idempotency guard (`set_tracer_provider()` no-op lần gọi sau)
- `pg_stat_statements` bật cho mọi Postgres (kể cả trong PL/pgSQL, `track=all`), xác minh
  bắt được câu `INSERT INTO sale` thật

### UI POS
- HTMX + Alpine.js vendor cục bộ (không CDN — đúng nguyên tắc #1: bán được khi mất mạng),
  giỏ hàng server-owned qua hidden field JSON, luồng đăng nhập → mở ca → quét mã vạch →
  thanh toán → hóa đơn chạy được đầu-cuối qua trình duyệt thật
- Một bug tiền thật tìm được bằng test tự động (`test_checkout_with_change_shows_correct_amount`):
  `sale_payment.amount` từng bị gán bằng tiền khách đưa thay vì tổng đơn — đã sửa

## 2. Dọn dẹp & sửa lỗi tồn đọng (2026-09-18)

- **Ryuk race điều kiện** (`testcontainers`) — flake tái diễn nhiều lần suốt phiên làm
  việc (`ConnectionError: Port mapping ... is not available`). Sửa tận gốc bằng
  `TESTCONTAINERS_RYUK_DISABLED=true`, áp dụng ở CI + `make test-all` + ghi vào
  `CLAUDE.md` để không bị phát hiện lại một cách đau đớn
- Quét `TODO/FIXME/XXX/HACK` toàn repo — chỉ còn đúng 1 điểm đã biết trước, có ghi chú rõ:
  `POST /ui/shifts/open` (route mở ca tạm bằng SQL trực tiếp, lịch thay bằng use case thật
  ở tuần 2 ngày 10, FR-P11)
- Đối chiếu import bên thứ ba dùng trong `packages/` với `pyproject.toml` — khớp hoàn toàn
- `git add -A -n` dry-run xác nhận không cuốn theo `.env` thật hay `__pycache__`

## 3. Trạng thái cổng kiểm tra (tại thời điểm tổng kết)

| Cổng | Kết quả |
|---|---|
| `ruff check` / `ruff format --check` | ✅ sạch |
| `mypy --strict` | ✅ 0 lỗi / 81 file |
| `import-linter` | ✅ 8/8 hợp đồng |
| `pytest` (unit + integration) | ✅ 220/220, không cần retry |
| Coverage domain/application | 90–100%, vượt ngưỡng 80% của cổng tuần 1 |
| `docker compose config` | ✅ hợp lệ trên cả 5 profile |

Quy mô code hiện tại: **~5.600 dòng** trong `packages/`, **16 file test**.

## 4. Demo chạy được ngay hôm nay

```bash
docker compose -f infra/compose.yaml --profile edge --profile central --profile observability up -d
```

1. **Bán hàng qua UI** — http://localhost:8001/ui/login → đăng nhập → mở ca → quét mã vạch
   thật (`8930001`...) → thanh toán tiền mặt, tính tiền thối đúng
2. **Cùng nghiệp vụ qua JSON API** — `POST /api/v1/sales` với JWT Bearer, đối chiếu DB thấy
   cùng bất biến với luồng UI
3. **Chống giả mạo employee_id** — JWT của nhân viên A, body ghi employee_id B → bị từ chối
4. **Trace thật** — Grafana (`:3001`, admin/admin) → Tempo → `service.name = edge-api-store-001`
5. **Query profiling thật** — `pg_stat_statements` trên `edge-db-store-001`
6. **Dữ liệu thật** — 5556 sản phẩm, 3515 cửa hàng từ `crawl_data/`, không phải seed giả

RAM đo được: **~1,13GiB** (edge + central + observability), dưới ngân sách 8GB nhiều.

### Chưa demo được (đúng lộ trình, thuộc tuần 2)
- Đồng bộ store → central qua outbox (`edge/sync/worker.py::run_forever()` cố ý
  `raise NotImplementedError`, container crashloop có chủ đích)
- `Central API POST /events` — logic idempotency đã có ở `central/ingest/idempotency.py`
  nhưng chưa có route FastAPI bọc ngoài
- Tra cứu khách hàng qua Redis + trung tâm (mới có bước 2/3 của chuỗi fallback, local Postgres)
- `POST /returns` / `/shifts` / `/reports` thật (đang dùng route mở ca tạm)
- Job đối soát, tầng data platform/warehouse (ClickHouse/MinIO/dbt/Airflow)

## 5. Việc vận hành phát sinh trong phiên demo

- Tài khoản `emp-007` trong DB đang chạy (volume giữ qua nhiều lần `docker compose down`
  không `-v`) là dữ liệu tạo thủ công ở phiên trước, không có trong script seed nào của
  repo — mật khẩu gốc không khôi phục được (Argon2 một chiều). Đã đặt lại thành `demo123`
  trực tiếp trên DB đang chạy để demo, xác minh bằng `POST /api/v1/auth/login` thật.
  **Không phải seed chính thức** — nếu cần tài khoản demo bền vững, nên viết script seed
  nhân viên thật (chưa có, có thể là việc nhỏ đầu tuần 2).
- User/password Postgres cho DBeaver/psql: `edge_app` / `changeme-edge` (port `5433`,
  db `edge_store_001`), `central_app` / `changeme-central` (port `5434`, db `central`) —
  lấy từ `infra/.env`, chỉ dùng cho dev.

## 6. Gợi ý việc đầu tiên của tuần 2

Theo [docs/06-roadmap.md](../06-roadmap.md) §Tuần 2, và theo thứ tự phụ thuộc tự nhiên:
1. `edge/sync/worker.py::run_forever()` — đẩy outbox thật (ADR-003)
2. `POST /events` ở Central API — bọc `central/ingest/idempotency.py` đã có sẵn
3. Tra cứu khách hàng đầy đủ 3 bước (Redis → Postgres cửa hàng → gọi trung tâm)
4. Thay route `/ui/shifts/open` tạm bằng use case mở ca thật (FR-P11)
5. `POST /returns`, `/shifts` (đóng ca), `/reports` vận hành

---
*Viết bởi Claude (Sonnet 5) theo yêu cầu tổng kết công việc, dựa trên các lần kiểm chứng
thật đã thực hiện trong phiên làm việc kết thúc 2026-09-18. Không phải tài liệu thiết kế —
xem `docs/00`–`16` cho nguồn sự thật thiết kế.*
