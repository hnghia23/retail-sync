# Giai đoạn B — bằng chứng chạy trên máy CI dùng một lần (2026-09-28)

> Tiếp nối [2026-09-25-giai-doan-b-chung-minh.md](2026-09-25-giai-doan-b-chung-minh.md) §8. Chủ dự án
> không muốn máy cá nhân chạy liên tục nhiều giờ. Phần việc còn lại **chạy được dưới 6 giờ** chuyển
> sang GitHub Actions (repo public: runner chuẩn 4 vCPU / 16 GB, miễn phí). Phần không chạy được ở đây
> (test ngâm 72h, LD-3, seam 50 triệu dòng ledger) chờ một VPS.

## 1. CI đỏ vì image MinIO — đã sửa, CI xanh

`quay.io/minio/minio` bắt đăng nhập (job `integration`: *unauthorized*). Máy dev vẫn chạy chỉ nhờ image
đã nằm trong cache. Chuyển sang bản build của Chainguard, ghim digest (lý do và cách cập nhật:
[ADR-005 §Rủi ro nguồn cung](../adr/005-clickhouse-warehouse.md)). Commit `f85edac`. **Cả 4 job xanh**
(`quality`, `unit`, `integration`, `scenarios`). Như vậy `scenarios` đã kiểm lại "`git clone` → chạy
được" trên máy không có cache image.

## 2. Workflow `proof` (`.github/workflows/proof.yml`)

Chỉ chạy bằng tay: Actions → **proof** → Run workflow → chọn nhóm (+ `-k` tùy chọn để chạy lại một
test). Mỗi nhóm chạy trên **một runner riêng, song song**. Runner dựng stack bằng
`infra/bootstrap.py --observability`, ghi nhật ký cảnh báo ở nền (`infra/alert_watch.py`), chạy test,
rồi hủy máy.

| Nhóm (`all` = 6 máy) | Chạy gì | Ước lượng |
|---|---|---|
| `drills` | 8 diễn tập ngắn: drift/quét toàn bộ, audit DIVERGED, dead-letter, partition, **`rs-horizon` (chạy lại)**, MinIO chết, DB cửa hàng chậm **đủ 16 phút (`rs-db-pool`)**, **DB trung tâm chậm** (`rs-ingest-p95`, `rs-store-lag` có chủ đích) | ≤ 2,5 giờ |
| `drill-store_offline_70min` | worker store-003 chết ~70 phút | ~1,5 giờ |
| `drill-transform_stalls` | **MỚI — `rs-transform`**: DAG tạm dừng, S4/S5 vẫn chạy từ ngoài (`pipeline run`) → đơn đã nạp mà chưa vào mart quá 70 phút | ≤ 2,5 giờ |
| `drill-pipeline_and_maintenance_stop` | **MỚI — `rs-loaded-until` + `rs-maintenance`**: DAG tạm dừng VÀ `central-maintenance` chết > 3 giờ | ~3,2 giờ |
| `load` | LD-1 + overhead OTel, LD-2 (10 → 200 cửa hàng ảo), LD-4 (**`rs-central-pg-conn`**) | ~1,5 giờ |
| `restore` | **MỚI — khôi phục thật** `store_backup.py restore` (§3) | ~15 phút |
| `chaos` (không nằm trong `all`) | CH-1…CH-7 trên máy sạch (đã đạt trên máy dev) | ~1,5 giờ |

Nguyên tắc giữ nguyên: **không hạ ngưỡng, không sửa rule** để nó kêu. Các ngưỡng tính bằng giờ nên các
diễn tập đó dài, và mỗi cái chiếm một máy.

**Kết quả** là artifact `proof-<nhóm>` (giữ 30 ngày): gồm `runs/alerts|load|chaos/<phiên>/*.json`, nhật
ký cảnh báo và bảng tổng kết (`report.txt`), bản ghi của `alert-sink`, `machine.txt`, `docker stats`, và
log compose khi đỏ. Khóa cửa hàng (`*.env`) không vào artifact.

**Hai điều phải nhớ khi đọc số:**
- Số đo tải (LD-1/2/4, overhead OTel) là của **runner 4 vCPU dùng chung**, không phải laptop. Luôn trích
  kèm `machine.txt`. Tiêu chí "bộ giả lập < 50% CPU" có thể đỏ trên máy 4 nhân khi cả stack chạy cùng
  máy. Nếu đỏ, đó là giới hạn của máy đo, không phải kết luận về hệ thống.
- LD-2/LD-4 cần 200 cửa hàng **có thật trong master data**. Bộ fixture chỉ có ~32, nên nhóm `load` sinh
  bộ tổng hợp lớn hơn: `infra/loadtest/synth_master.py` = fixture + 260 cửa hàng tỉnh mẫu C (tổng 292).
  Không có nó, bậc "200" âm thầm chỉ đo ~29 cửa hàng.

## 3. Test khôi phục thật — `tests/scenarios/test_restore.py`

Trước đây chỉ có `verify` (khôi phục thử vào database tạm). Test mới thay database THẬT của store-001:

1. Bán B khi sync worker tắt → backup ngay (khởi động lại sidecar) → lúc backup, sự kiện của B còn chưa gửi.
2. Bật worker, B lên trung tâm. `verify` bản backup.
3. Bán C sau backup, đồng bộ xong.
4. `restore`. Kiểm: C **mất ở cửa hàng** nhưng **còn đủ ở trung tâm** (RPO); sự kiện B được gửi LẠI
   từ outbox đã khôi phục nhưng đối soát vẫn `CONVERGED` (idempotency, AT-03); **bán tiếp được ngay**
   (E) và E lên trung tâm bình thường.

Opt-in bằng `RETAIL_SYNC_CHAOS=1` (thay database thật).

## 4. Trạng thái

Code + workflow xong, **chưa chạy lần nào**: các test mới chưa từng chạy trên Docker thật. Cần chủ dự án
push rồi bấm chạy. Kết quả sẽ ghi vào đây và vào bảng cảnh báo của nhật ký 2026-09-25 §5.

Sau `proof`, việc còn lại của cổng B: LD-3 (bản dbt cũ và mới), seam 50 triệu dòng ledger, ghi số vào
docs/02, test ngâm 72h. Các việc này cần một máy chạy liên tục (VPS) hoặc máy dev lúc rảnh.
