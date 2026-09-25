# Tiến độ 2026-09-24 — giai đoạn A: lake + bronze (S4, S5)

> Nhật ký tiến độ, không phải docs thiết kế. Nối tiếp
> [2026-09-23-tuan-2-dong-bo.md](2026-09-23-tuan-2-dong-bo.md) §8. Hướng đi: [ADR-010](../adr/010-data-flow-first.md),
> bản đồ luồng: [docs/17](../17-data-flow.md).

## 1. Đã làm

- **`packages/pipeline/`**: chặng S4 (Postgres trung tâm → Parquet trên MinIO) và S5 (Parquet →
  `bronze_*` trong ClickHouse). Package độc lập, không import `shared`/`edge`/`central`/
  `simulator` (hợp đồng `import-linter` mới), vì nó còn phải chạy trong image Airflow có bộ
  dependency riêng. Chỉ cần asyncpg + pyarrow + httpx.
  - `tables.py`: mỗi bảng khai báo MỘT lần cả SQL nguồn, kiểu Parquet và biểu thức nạp.
    Có 7 bảng incremental (sale, sale_line, sale_payment, point_ledger, shift, customer,
    point_balance) và 6 bảng snapshot master data. Không PII, không `password_hash`.
  - **Lake bất biến là trạng thái.** Cửa sổ mới bắt đầu ở mép cuối của file cuối cùng, và
    file đã có thì không bao giờ trích lại. Không cần bảng watermark; đổi độ dài cửa sổ giữa
    chừng cũng không tạo chồng lấn.
  - Mép cửa sổ ≤ `extract_horizon()` (bẫy 1). Đọc bằng cursor, 50k dòng mỗi nhóm Parquet,
    `ORDER BY` cố định, nên cùng dữ liệu cho ra cùng bytes và cùng SHA-256.
  - Nạp: token = đường dẫn + SHA-256, đọc bằng `max_threads=1`, ghi `bronze_load_log`
    (nguồn của DI-4 và mép "đã nạp tới").
- **DDL bronze** chuyển từ `data_platform/clickhouse/` về `packages/pipeline/ddl/`, thêm 6 bảng
  snapshot + `bronze_load_log`.
- **Audit L3** (`simulator audit --clickhouse …`): đơn thiếu ở bronze mà nằm dưới mép "đã nạp
  tới" là mất, nằm trên mép là chưa tới; cùng phiên bản hai lần là khử trùng hỏng.
- Compose: MinIO + ClickHouse chạy thật (`--profile data up minio clickhouse`). Makefile:
  `pipeline-init`, `pipeline-run`.

## 2. Phát hiện thật

| # | Cái gì | Xử lý |
|---|---|---|
| 1 | **Docker Hub `minio/minio` không còn tồn tại** — pull trả "repository does not exist". Compose sẽ chết ngay ở máy mới | Chuyển sang `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z`, ghim bản; ghi rủi ro nguồn cung vào ADR-005 |
| 2 | **Bỏ token khử trùng mà bronze vẫn không nhân đôi** (kiểm đột biến): có cửa sổ khử trùng thì ClickHouse còn khử trùng theo hash nội dung khối | Không phải lỗi — là lớp bảo vệ thứ hai. Nhưng bảng có `DEFAULT now64()` (nhật ký nạp) chỉ token cứu được; thêm test chứng minh phân biệt đó, ghi vào docs/17 bẫy 4 |
| 3 | Test "file cũ bất biến" bản đầu **không bắt được** bộ trích xuất không giữ trạng thái, vì dòng bị UPDATE là dòng duy nhất của cửa sổ | Viết lại test: cửa sổ cũ còn dữ liệu khác, và đột biến bị bắt |
| 4 | Compose Airflow dùng `airflow webserver`, lệnh đã bị bỏ ở Airflow 3 (thay bằng `api-server`), và thiếu `dag-processor` bắt buộc | Chưa sửa — làm khi dựng Airflow (việc tiếp theo) |
| 5 | pyarrow mặc định cấm tạo bucket khi ghi | Chỉ `ensure_bucket()` (lệnh `init`) được tạo; đường ghi dữ liệu vẫn cấm, để sai tên bucket thì phải lỗi |

## 3. Kiểm chứng

- 10 test mới: `test_pipeline.py` 7, test hash-vs-token 1, bộ giả lập bốn tầng 2. Toàn repo
  **366/366**; ruff, `mypy --strict`, **12/12** hợp đồng import sạch.
- **Kiểm đột biến 3/3:** mép cửa sổ theo `now()` thay vì `extract_horizon()`; bỏ token (bị bắt
  sau khi hiểu ra phát hiện #2: test phân biệt bảng có `DEFAULT now64()`); bộ trích xuất không
  giữ trạng thái, ghi đè file (bị bắt sau khi sửa test, phát hiện #3).
- **Trên compose thật:**
  - `pipeline run` 25 giây: 14–15 cửa sổ một giờ mỗi bảng + 6 snapshot (3516 cửa hàng, 5556
    sản phẩm); 213 đơn, 697 dòng hàng, 221 dòng thanh toán, 126 dòng điểm vào bronze.
  - **Hai lần chạy bộ giả lập hôm qua, audit bốn tầng:** `live-t0-1` 102 đơn / 317.619.500đ /
    19.402 điểm, `live-t0-2` 102 đơn / 55.316.000đ / 2.061 điểm. L0 = L1 = L2 = L3 → `CONVERGED`.
  - Chạy lại pipeline: 0 dòng mới, số đếm giữ nguyên.
  - Xóa sạch 14 bảng bronze trong ClickHouse → chạy lại pipeline → **dựng lại đúng số cũ** chỉ
    từ lake → audit vẫn `CONVERGED`.

## 4. Tiếp theo (giai đoạn A)

> Mục 1–3 đã xong cùng ngày, xem [2026-09-24-giai-doan-a-dbt-airflow.md](2026-09-24-giai-doan-a-dbt-airflow.md).

1. **Airflow 3** lên compose: sửa `api-server` + `dag-processor`, image chứa `packages/pipeline`
   (thêm asyncpg/httpx vào `data_platform/requirements.txt`), DAG chạy `pipeline run` theo giờ.
   Spike S1 (RAM cả stack) và S3 (cosmos).
2. **dbt S6**: staging (silver: bản mới nhất theo khóa) → `dim_*` → `fact_sale_line`,
   `fact_payment` riêng, `fact_point_event`; **bẫy 5** (tính lại đúng tháng bị ảnh hưởng).
3. **Audit L4** (marts) → đủ điều kiện cổng A.
4. Bộ giả lập chế độ `virtual` + tật `offline`, `resend`, `concurrent_customer`.

---
*Viết bởi Claude (Opus 5.5) theo các lần kiểm chứng thật trong phiên 2026-09-24.*
