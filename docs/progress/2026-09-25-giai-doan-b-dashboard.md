# Tiến độ 2026-09-25 — Giai đoạn B, việc 1: dashboard "sức khỏe luồng"

> Nhật ký tiến độ, không phải docs thiết kế. Nối tiếp [2026-09-24-cong-a.md](2026-09-24-cong-a.md) §3
> (nợ từ A). Định nghĩa chỉ số: [17 §6](../17-data-flow.md). Công cụ: [10 §9](../10-observability.md).

**Xong.** Mọi chỉ số ở docs/17 §6 giờ là metric Prometheus thật, nằm trên một dashboard Grafana
provision từ repo. Kiểm trên compose bằng 20 cửa hàng ảo: dashboard ghi lại đúng từng bước của
một lượt luồng, và audit L0…L4 `CONVERGED` với cùng con số.

## 1. Đã làm

| Phần | Ở đâu | Ghi chú |
|---|---|---|
| `MeterProvider` dùng chung | `shared/metrics.py` | Cùng khuôn với `shared/tracing.py`; bật semconv HTTP ổn định |
| S1, S3 độ trễ request | instrumentation FastAPI | `http_server_request_duration_seconds{http_route}`, giây, bucket 5 ms…10 s |
| S2 outbox (độ sâu, tuổi cũ nhất, dead-letter) | `edge/health.py` `OutboxGauges` | Đo ở **edge-api**, không ở worker (worker chết đúng là lúc outbox dồn). Hai câu lệnh khớp hai partial index |
| S2 sync worker | `edge/sync/telemetry.py` | Lỗi đường truyền và bị trung tâm từ chối là **hai** chỉ số (quy tắc AT-02) |
| S3 kết cục từng sự kiện | `central/ingest/telemetry.py` + `IngestStats` | `accepted` / `duplicate` / `rejected_*` / lô bị từ chối (503, 413). Đếm SAU commit |
| S3–S6 trạng thái, độ tươi L2/L4, lệch INV-4 | `pipeline/monitor.py`, service `flow-monitor` | `python -m pipeline health` (JSON một lần) / `monitor` (OTLP mỗi 30 s). Chỉ đọc, chạy ngoài DAG |
| Chênh đối soát | `simulator/telemetry.py`, `audit --otlp`, `--watch N` | Audit thêm khối `freshness` theo tầng (`freshness_seconds`, `behind_seconds`) |
| Dashboard | `infra/observability/grafana/dashboards/flow-health.json` | 29 panel: ô "phải về 0" → S1 → S2 → S3 → S4–S6 → toàn luồng |
| Compose | `infra/compose.yaml` | `otel-lgtm` ghim `0.33.0` + volume `/data` + provision; `flow-monitor`; `OTEL_METRIC_EXPORT_INTERVAL=15000` |

## 2. Kiểm chứng trên compose

Stack: `edge` + `edge-multi` + `central` + `data` + `observability`. Bộ giả lập `virtual`, profile
`t1`, 20 cửa hàng, 2 ngày giả lập, tật `offline` + `resend` + `concurrent_customer`
(`run_id=flow-health-v1`):

- 12.235 đơn, 19.450 sự kiện, 321 lô gửi lại, 58 lỗi đường truyền lúc 5 cửa hàng offline,
  0 dead-letter. CPU bộ giả lập 5,5%.
- Dashboard ghi lại cả lượt (truy vấn lịch sử Prometheus):

| Giờ (UTC) | Chuyện gì xảy ra | Dashboard thấy |
|---|---|---|
| 02:54–03:02 | Bộ giả lập đẩy | `ingest_events_total{accepted}` 12 → **52 sự kiện/s**; `POST /events` p95 0,29 s, **p99 1,64 s**; có lô bị từ chối `overloaded` (503) |
| 03:01–03:04 | DAG lần 1 (cửa sổ 1 giờ) | S6 `pending` **2.764** → 0 |
| 03:06 | Audit, `--wait 60` | `audit_status` = 2 (`DIVERGED`) |
| 03:06 | S4+S5 cửa sổ 5 phút từ host | trích đúng **9.471** đơn |
| 03:07–03:09 | DAG lần 2 | S6 `pending` **9.471** → 0 |
| 03:13 | Audit lại | `audit_status` = 0, `behind` = 0 ở mọi tầng |

`DIVERGED` lúc 03:06 là **đúng định nghĩa**, không phải lỗi. Cả 9.471 phát hiện là `CONVERGING`
(đơn tới trung tâm sau 03:00, cửa sổ `[03:00, 04:00)` chưa đóng), nhưng `--wait 60` ngắn hơn
một chu kỳ DAG, nên quá hạn mà vẫn thiếu thì thành sự cố. Bài học vận hành: **`--wait` của audit
L3/L4 phải dài hơn một chu kỳ DAG cộng cửa sổ trích xuất.**

Kết quả cuối, audit `CONVERGED`: L0 = L1 = L2 = L3 = L4 = **12.235 đơn / 6.686.186.090đ**.

Hai tín hiệu tải nay đã nhìn thấy trên dashboard:
- **p99 1,64 s của `POST /events`** (phía server), đúng cái đuôi 1,65 s để lại từ cổng A. Phía
  client p99 632 ms, vì client chỉ đo những lô trung tâm đã nhận. Việc săn thuộc LD-2.
- **Backpressure thật sự kích hoạt** ở 20 cửa hàng: `ingest_max_concurrency = 8` từ chối lô bằng
  503, cửa hàng lùi rồi gửi lại. Không mất gì (0 dead-letter, audit khớp).

## 3. Lỗi lộ ra khi làm

| # | Lỗi | Sửa |
|---|---|---|
| 1 | `/health/outbox` đếm cả dead-letter vào "đang chờ": chỉ một sự kiện dead-letter là `oldest_unsent_age_seconds` (cảnh báo quan trọng nhất) tăng mãi | Hai câu lệnh trên hai partial index; `dead_lettered` tách riêng. Test kiểm bằng đột biến |
| 2 | **Mật khẩu ClickHouse nằm trong URL** (`?password=…`), httpx ghi URL ở INFO → `flow-monitor` in mật khẩu ra `docker logs` ngay lần chạy đầu. Cùng lỗi ở client của audit | Đăng nhập bằng header `X-ClickHouse-User`/`X-ClickHouse-Key`. Log task Airflow đã kiểm: chưa có dòng nào lộ |
| 3 | Job đối soát INV-4 **chưa bao giờ được lên lịch**: chỉ chạy tay/test. Dashboard lộ ra ngay (`reconcile_last_run_age_seconds` = 16 giờ). "Lệch = 0" của job không chạy là con số vô nghĩa | `central.ops.reconcile --every`, service `central-reconcile` (mỗi giờ, `RECONCILE_INTERVAL_SECONDS`) |
| 4 | Thuộc tính resource `store.id` không thành label trong Prometheus; histogram mặc định 0…10000 vô nghĩa cho giây | `store_id` là attribute từng điểm đo; độ trễ lấy từ semconv HTTP ổn định ([10 §9](../10-observability.md)) |
| 5 | Counter sync worker chỉ xuất hiện khi có sự kiện đầu → panel "No data" trông như worker chết | Khởi tạo bằng `add(0)` |

## 4. Test

- Mới: `tests/unit/test_metrics.py`, `test_flow_dashboard.py` (mọi tên metric trong dashboard phải
  do code phát — dựng danh mục từ chính instrument thật), `test_clickhouse_credentials.py`,
  `tests/integration/test_flow_monitor.py` (6, gồm S6 với **dbt thật** và transaction treo giữ
  `extract_horizon`), `test_flow_metrics.py` (3). Audit: độ tươi L1…L4 `behind = 0` trong
  `test_simulator_edge.py`.
- Kiểm đột biến: gộp dead-letter vào outbox, đếm trùng như mới, mép S6 `>=`, gõ sai tên metric
  trong dashboard — cả bốn đều làm test đỏ.

- Toàn repo: **416 passed + 7 skipped** (7 = `tests/scenarios/` opt-in), ruff, `mypy --strict`,
  12/12 hợp đồng import sạch.
- Lần chạy cả suite ĐẦU TIÊN (lúc máy đang chạy song song bộ giả lập 20 cửa hàng + DAG) có 2 test
  đỏ, chạy riêng và chạy lại cả suite đều xanh:
  `test_refused_batch_is_counted_and_not_recorded_as_events` (test mới của phiên này: một
  `except Exception: pass` nuốt mất nguyên nhân → viết lại thành gọi HTTP thẳng và kiểm `503`), và
  `test_virtual_stores_with_all_gate_a_quirks_converge_at_central` (có từ cổng A, đòi cửa hàng
  offline gặp ≥ 1 lỗi đường truyền — nhạy thời gian khi CPU bị tranh). Ghi lại để test ngâm/CI
  để ý: test này có thể là test chập chờn dưới tải.

## 5. Chưa làm

- **S1/S2 phía cửa hàng thật chưa có tải**: chạy bộ giả lập chế độ `edge` cần `SEED_EMPLOYEE_PASSWORD`,
  không lưu ở đâu trong repo. Đường ống metric đã kiểm (request `401` vào `/api/v1/sales` hiện đủ
  ở cả 3 cửa hàng); số thật có ở lần chạy `edge` kế tiếp.
- Metric của Airflow (thời lượng DAG, task lỗi) chưa đẩy OTLP. Thời lượng từng model dbt vẫn xem
  ở Airflow UI (cosmos).
- S5 "số khối bị khử trùng" (`system.query_log`) chưa có trên dashboard.
- Ngưỡng cảnh báo (roadmap ngày 19): dashboard vẽ ngưỡng thành đường nét đứt, **chưa** có alert
  rule. Ngưỡng độ tươi phải tính giờ mở cửa.
