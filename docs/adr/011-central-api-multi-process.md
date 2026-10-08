# ADR-011 — Central API chạy nhiều tiến trình

- **Trạng thái:** ✅ Chấp nhận (2026-10-08), ✅ **đã đo** (workflow `proof` 2026-10-08, §Kết quả đo)
- **Người quyết định:** chủ dự án
- **Liên quan:** [08-reliability-and-scale §3.2](../08-reliability-and-scale.md) (backpressure),
  [02-scale-capacity](../02-scale-capacity.md), [ADR-003](003-outbox-not-kafka.md) (Outbox + HTTP),
  [ADR-009](009-observability-stack.md) (overhead OTel),
  [progress 2026-09-28 → 10-07](../progress/2026-09-28-giai-doan-b-proof-ci.md)
- **Không thay thế ADR nào.** Vẫn một Central API, một Postgres. ADR này đổi **số tiến trình** của API.

---

## Bối cảnh

LD-2 (10 → 200 cửa hàng ảo cùng đẩy đồng bộ) chạy trên runner CI 4 vCPU, workflow `proof` 2026-10-07:

| Sự kiện/giây | p95 `POST /events` | CPU central-api (TB / đỉnh) | CPU central-db (TB / đỉnh) |
|---|---|---|---|
| 28 | 0,07 s | 8% / 28% | 3% / 13% |
| 127 | **1,08 s** | 41% / **102%** | 13% / 51% |
| 222 | **1,69 s** | 66% / **102%** | 20% / 38% |
| 283 | **1,68 s** | 90% / **103%** | 26% / 49% |

Tải thiết kế ở [02 §1](../02-scale-capacity.md) là ~260 lượt ghi/giây (T3, đỉnh lễ Tết). Từ ~127
sự kiện/giây trở lên, central-api chạm **đúng một nhân**: nó là MỘT tiến trình uvicorn, mọi request
chạy trên một event loop Python. Postgres còn dư 2–4 lần. Dữ liệu thì luôn đúng: mọi bậc
`CONVERGED`, mọi từ chối là `429`/`503` có `Retry-After`. Cái hỏng là độ trễ.

Trace 100% làm nút thắt nặng thêm: ở edge-api, CPU tăng từ 7,9% lên 12,7% (đo overhead OTel cùng
lượt). Nhưng giảm lấy mẫu chỉ dời điểm gãy, không gỡ nó. Ngân sách ổn định đứng trên tính năng
(CLAUDE.md), nên chủ dự án chọn gỡ trần một nhân.

## Quyết định

`central-api` chạy **N tiến trình uvicorn** (`--workers N`, mặc định 4, biến `CENTRAL_API_WORKERS`).

Mọi thứ giả định "một tiến trình" được xử lý tường minh (`central.settings.worker_budget()`):

| Thứ theo tiến trình | Trước | Sau | Vì sao |
|---|---|---|---|
| Semaphore `503` (`INGEST_MAX_CONCURRENCY` = 8) | 8 | **8 / N** mỗi tiến trình | Nó bảo vệ POSTGRES: tổng phải giữ nguyên, không nhân N |
| Pool DB (10 + 10 overflow) | 20 | **⌈10/N⌉ + ⌈10/N⌉** mỗi tiến trình | `max_connections` = 100 là trần CHUNG (LD-4) |
| Rate limit theo cửa hàng `429` | 10 req/s | **10 req/s, KHÔNG chia** | Sync worker giữ kết nối keep-alive, nên mọi lô của một cửa hàng thường vào CÙNG một tiến trình. Chia cho N thì cửa hàng chỉ còn 1/N hạn mức. Trần thực tối đa là N × rate, điều `central.ingest.ratelimit` đã chấp nhận từ đầu (mục tiêu là chia lượt, không phải hạn ngạch chính xác) |
| Metric OTel | `service.name` | + **`service.instance.id`** (host + pid) | Thiếu nó, N tiến trình đè lên MỘT series trong Prometheus: counter nhảy lùi, gauge chỉ còn tiến trình đẩy sau cùng. Truy vấn dashboard/cảnh báo đều gộp (`sum`/`max`) nên không đổi |
| `db_pool_used_ratio` | chia cho 20 | chia cho **sức chứa thật** của pool tiến trình đó | Không thì pool 6 kết nối đầy chỉ báo 30% |

`--workers` và `CENTRAL_API_WORKERS` phải là CÙNG một số (compose đọc cùng biến cho cả hai).

## Phương án đã cân nhắc

1. **Chỉ giảm lấy mẫu trace (ADR-009).** Rẻ, đảo ngược được. Nhưng chỉ dời điểm gãy: Python một
   tiến trình vẫn là trần, và overhead còn lại phần lớn đến từ metric + instrumentation, không
   tắt được (observability là Must). Vẫn nên làm theo ADR-009, nhưng việc đó tách riêng.
2. **Nhiều tiến trình + rate limit qua Redis** (hạn ngạch chính xác trên mọi tiến trình). Thêm một
   phụ thuộc mạng trên đường ingest nóng (Redis chết → fail-open hay fail-closed?), trong khi chưa
   có yêu cầu nào cần hạn ngạch chính xác. **Để làm scale seam**: khi có yêu cầu đó, hoặc khi
   chạy nhiều MÁY Central API sau load balancer (lúc đó keep-alive không còn gom một cửa hàng về
   một chỗ).
3. **Chấp nhận, đo lại trên VPS.** Runner chia 4 nhân cho ~30 container nên số đo bi quan. Nhưng
   một tiến trình Python thì máy nào cũng chỉ có một nhân cho nó, nên VPS không gỡ được trần.

## Hệ quả

- (+) Central API dùng được nhiều nhân. Tổng kết nối Postgres không đổi.
- (−) Mỗi tiến trình ~100 MB RAM. 4 tiến trình vẫn vừa ngân sách 16 GB của laptop.
- (−) `503` đến sớm hơn một chút khi tải lệch: semaphore chia đều cho N nhưng keep-alive làm tải
  không đều. Cửa hàng không mất gì (outbox, `Retry-After`).
- (−) Ngưỡng `rs-db-pool` (80%) giờ đo trên pool nhỏ (6 kết nối với N = 4), nên dễ chạm hơn.
  Theo dõi ở test ngâm.
- **Điều kiện xem lại:** LD-2 chạy lại vẫn > 1 s ở tải thiết kế, và CPU central-api KHÔNG còn
  chạm trần → nút thắt đã dời sang chỗ khác (DB, lock), cần đo lại. Hoặc khi chạy nhiều máy
  Central API → phương án 2.

## Kết quả đo (workflow `proof`, 2026-10-08, cùng runner)

| Tải đặt vào (sự kiện/giây) | p95 — 1 tiến trình | p95 — 4 tiến trình | CPU central-api (đỉnh) |
|---|---|---|---|
| ~136 | 1,08 s | **0,16 s** | 102% → 131% |
| ~273 (≈ thiết kế) | 1,69 s | **0,99 s** | 102% → 255% |
| ~543 (2× thiết kế) | 1,68 s | 1,56 s | 103% → 267% |

Ở 2× tải thiết kế, runner 4 nhân hết CPU cho cả stack (central-api 2,7 nhân + Postgres 1 nhân). Theo
tiêu chí LD-2 hai vùng (docs/16 §4), bậc đó chỉ đòi từ chối tử tế + không mất dữ liệu, và đạt.
`rs-db-pool` (pool nhỏ hơn: 6 kết nối mỗi tiến trình) sang `pending` 3 lần ở hai bậc nặng nhưng chưa
lần nào giữ đủ 5 phút để kêu. Ở tải thiết kế, pool một tiến trình đã sát 80%: theo dõi ở test ngâm,
và là ứng viên đầu tiên khi cần nới (pool lớn hơn cho mỗi tiến trình, vẫn trong `max_connections`).

---
*Changelog: 2026-10-08 (lần 2) — kết quả đo.*

*Changelog: 2026-10-08 — tạo mới.*
