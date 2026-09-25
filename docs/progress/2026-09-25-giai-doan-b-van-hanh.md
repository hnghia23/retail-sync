# Tiến độ 2026-09-25 (lần 3) — Giai đoạn B: rà soát vận hành, vá các lỗ "im lặng tới phút cuối"

> Nhật ký tiến độ, không phải docs thiết kế. Nối tiếp
> [2026-09-25-giai-doan-b-ci.md](2026-09-25-giai-doan-b-ci.md).

Chủ dự án hoãn test ngâm 72h (máy local cần cho việc khác) và yêu cầu rà soát những gì còn cải
thiện được, **làm trước, chưa cần test thực tế**. Rà soát tập trung vào các việc Must của roadmap
ngày 19–20 và vào những chỗ hệ thống có thể hỏng MÀ KHÔNG BÁO GÌ.

## 1. Kết quả rà soát và đã làm

| # | Vấn đề | Mức | Đã làm |
|---|---|---|---|
| 1 | **Không có gì gọi hàm tạo trước partition `point_ledger`** (ràng buộc #2). Docstring ghi "gọi từ Airflow", DAG chưa bao giờ gọi. Partition có tới 2026-12-01 → mọi sự kiện có điểm bị từ chối từ 00:00 ngày 01/01/2027 | 🔴 | `central.ops.maintenance` + service `central-maintenance` (mỗi giờ: partition rồi đối soát INV-4, hai việc độc lập). Metric `point_ledger_partition_months_ahead`, ô dashboard, cảnh báo < 2 tháng. Thay service `central-reconcile` của lần trước |
| 2 | Không có job dọn `outbox` đã gửi (ràng buộc #9) | 🔴 | `prune_sent()` trong sync worker: mỗi giờ, theo lô, chỉ dòng ĐÃ GỬI quá `SYNC_OUTBOX_RETENTION_DAYS` (7). Không bao giờ đụng dòng chưa gửi hay dead-letter. Metric `sync_outbox_pruned_total` |
| 3 | S4 liệt kê TOÀN BỘ lake mỗi lượt để tìm mép cửa sổ cuối (~9 nghìn file/bảng/năm) | 🟠 | Dùng `Lake.latest_files` (chỉ phân vùng `dt=` mới nhất) |
| 4 | Log chưa ở dạng JSON có `trace_id` (roadmap Must) | 🟠 | `shared/logs.py`: structlog `ProcessorFormatter` render MỌI dòng stdlib (cả uvicorn) thành JSON + `trace_id`/`span_id`. Đúng stack đã chốt (docs/07), code không đổi cách gọi log |
| 5 | Chưa có alert rule nào | 🟠 | 15 rule Grafana provision từ repo, ngưỡng docs/08 §5. Test đỏ nếu rule dùng metric không ai phát |
| 6 | Chưa có backup/khôi phục Postgres cửa hàng (RPO ≤ 24h, B02 I06) | 🟠 | Sidecar `edge-backup-*` (dump ngay khi khởi động rồi mỗi ngày, giữ 7, ghi file tạm rồi đổi tên). `infra/store_backup.py list/verify/restore` |

## 2. Kiểm chứng (nhẹ, không phải test thực tế)

- Unit + integration cho từng phần: `test_maintenance.py` (xóa 3 partition tương lai → một lượt trả
  đủ; partition lỗi không kéo đối soát chết), `test_flow_metrics.py::test_prune_…` (4 loại dòng, chỉ
  "đã gửi quá hạn" bị xóa, lô nhỏ đi qua nhiều vòng), `test_logs.py`, `test_store_backup.py`,
  `test_flow_dashboard.py` (thêm cảnh báo). `test_pipeline.py` 10/10 với `latest_files`.
- Stack dev khởi động lại với code mới: 4 API `200`; log JSON (access log mang `trace_id`);
  `central-maintenance`: "partition[OK] 5 bảng, tới 2026-12-01, đệm 3 tháng · reconcile drift=0";
  Grafana nạp 15 rule, cả 15 đánh giá được (`ok`, `inactive`).
- `store_backup.py verify --store store-001`: khôi phục bản dump 356 KB vào DB tạm, **7 bảng khớp
  tuyệt đối** với DB đang chạy (515 đơn, 1671 dòng hàng, 863 outbox…). DB thật không bị đụng.

## 3. Chưa làm (đợi test thực tế)

- Kích hoạt thử từng cảnh báo (roadmap: "kích hoạt thử được") — làm cùng test hỗn loạn CH-1…7.
  Chưa có contact point (cảnh báo chỉ hiện ở Grafana → Alerting).
- `store_backup.py restore` trên DB cửa hàng thật chưa chạy thử.
- Partition "chỉnh đồng hồ tới tháng sau" chưa chạy.
- Bộ giám sát (`pipeline`) vẫn log dạng chữ: package độc lập, không import `shared`.
- Nhiễu log lỗi export OTLP khi không bật profile `observability`.
- Test ngâm 72h: hoãn theo quyết định của chủ dự án.
