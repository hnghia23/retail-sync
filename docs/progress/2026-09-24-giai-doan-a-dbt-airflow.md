# Tiến độ 2026-09-24 (lần 2) — giai đoạn A: sửa lỗi, dbt S6, Airflow 3, audit L4

> Nhật ký tiến độ, không phải docs thiết kế. Nối tiếp
> [2026-09-24-giai-doan-a-lake.md](2026-09-24-giai-doan-a-lake.md) §4. Hướng đi:
> [ADR-010](../adr/010-data-flow-first.md), bản đồ luồng: [docs/17](../17-data-flow.md).

**Kết quả chính:** luồng S1 → S6 chạy hết trên compose. Một đơn bán qua Edge API đi tới
`fact_sale_line` bằng DAG Airflow theo lịch. Bộ đối soát thấy **L0 = L1 = L2 = L3 = L4** cho cả
ba lần chạy bộ giả lập, kể cả sau khi xóa sạch ClickHouse và dựng lại từ lake.

## 1. Sửa lỗi trước

| # | Lỗi | Hậu quả nếu để | Sửa | Test |
|---|---|---|---|---|
| 1 | Compose Airflow dùng `airflow webserver` (Airflow 3 đã bỏ) | Airflow không lên | `api-server` | chạy thật |
| 2 | Thiếu `dag-processor` | Không DAG nào được đọc | thêm service | chạy thật |
| 3 | Không đặt JWT chung + `execution_api_server_url` | Mỗi tiến trình tự sinh khóa → mọi task chết "Signature verification failed" | `AIRFLOW_JWT_SECRET` (bắt buộc) + URL về api-server | chạy thật |
| 4 | Image Airflow không có `packages/pipeline`; dbt cài chung môi trường Airflow, không constraint | DAG không import được pipeline; dependency vỡ khi nâng bản | pipeline cài kèm constraint chính thức, dbt ở venv riêng | chạy thật |
| 5 | **Hai lượt pipeline chạy chồng nhau** (DAG + `make pipeline-run`): "kiểm tồn tại rồi ghi" không nguyên tử | Lượt sau ghi đè file bằng nội dung khác (nếu có UPDATE ở giữa). Lake ≠ bronze, dựng lại từ lake mất phiên bản cũ | Advisory lock cấp phiên trên PG trung tâm, giữ qua cả trích lẫn nạp; lượt sau dừng ngay (`PipelineBusyError`, mã thoát 75) | 2 test, **đột biến 2/2** |
| 6 | **Bảng incremental rỗng không sinh file** | Vắng khỏi nhật ký nạp → mép "đã nạp tới" của audit L3 là NULL: mọi đơn mất thật bị báo "chưa tới". Mép của dbt tắc vĩnh viễn | File rỗng làm mốc cho cửa sổ ngay trước `floor(horizon)` | 1 test, **đột biến 1/1** |
| 7 | Không có `.dockerignore`, build context là cả repo | `infra/.env` (secret) và `.venv` 250 MB bị gửi vào builder mỗi lần build; một dòng `COPY . .` là secret nằm trong image | `.dockerignore` | — |
| 8 | CLI chết `UnicodeEncodeError` khi output bị chuyển hướng trên Windows (cp1252), kể cả `--help` | `... \| tee log` là CLI chết | `shared.console.utf8_stdio()` ở 6 CLI | 6 test (ép `PYTHONIOENCODING=cp1252`) |

## 2. Đã làm

- **dbt S6** (`data_platform/dbt/`): 13 model staging (silver: bản mới nhất theo khóa, chỉ phần
  bronze dưới mép "đã nạp tới"), 6 dim, 3 fact tăng dần theo bẫy 5, 32 test. Chi tiết thiết kế ở
  [docs/17 §4 bẫy 5](../17-data-flow.md) và [docs/05 §7](../05-data-model.md) changelog.
- **DAG `retail_pipeline`** (Airflow 3.3.2 + cosmos 1.15.1): `bronze_ddl → extract_load → dbt.*`,
  mỗi model một task `run` + `test`. Lịch `PIPELINE_SCHEDULE`, `catchup=False` (lake là trạng
  thái), `max_active_runs=1`.
- **Audit L4** (`simulator audit --clickhouse … --marts`): đọc mart qua `dim_store`/`dim_shift`,
  so với L3.
- `make dbt-build`, `make dag-run`; `make up-data` thêm `--build`; `make sim-audit CLICKHOUSE=…`
  bật L3 + L4. `data_platform/requirements-dbt.txt` là chỗ ghim bản dbt duy nhất.

## 3. Phát hiện thật

| # | Cái gì | Xử lý |
|---|---|---|
| 1 | **Cổng A ghi sai công thức:** "Σ `fact_payment` = Σ `line_total`". Thanh toán bằng tổng SAU chiết khấu, còn `line_total` là tổng trước chiết khấu | Fact có `discount_allocated` (phân bổ theo tỉ lệ, phần dư dồn dòng cuối) và `net_amount`. Cổng A sửa thành Σ `net_amount`. Test: chiết khấu 1.801đ chia 3 dòng không chia hết, khớp tới từng đồng |
| 2 | **dbt lần đầu đỏ 2 test:** 7 đơn tuần 1 trỏ tới `emp-007`/`SKU-001` (dữ liệu demo), không có trong master data | Trung tâm cố ý nhận đơn không cần khóa ngoại (cửa hàng tự chủ), nên mart phải chịu được: **inferred member** (`is_inferred = 1`) + test `warn`, không chặn luồng |
| 3 | **Bẫy alias của ClickHouse:** `assumeNotNull(x) AS x … WHERE x IS NOT NULL` kiểm chính alias, không kiểm cột, nên NULL lọt qua thành UUID toàn số 0 (một "khách inferred" ma) | Lọc trong subquery trước. Ghi thành quy tắc ở `data_platform/README.md`; test giữ "khách vãng lai không thành khách inferred" |
| 4 | **Mép cắt phải tính một lần mỗi model.** Nếu view staging tự tính thì "tập tháng" và "dữ liệu" có thể thấy hai mép khác nhau khi có nạp chen giữa, và dòng ở giữa bị bỏ qua vĩnh viễn | `pipeline_cutoff()` tính bằng `run_query`, nhúng hằng vào SQL |
| 5 | **Tôi đã hiểu sai lý do của mép "đã nạp tới"** (comment ban đầu nói thiếu mép thì mất dòng vĩnh viễn). Test chỉ ra: nạp dở ở mức bảng thì tự lành. Mép giữ **nhất quán giữa các fact**; thiếu nó thì test cổng A đỏ giả | Sửa comment + docs; đột biến "bỏ mép" bị test nạp dở bắt |
| 6 | Cosmos 1.15 mặc định `DBT_RUNNER`, từ chối đường dẫn dbt riêng; mặc định bỏ test trên source; gắn test nhiều cha vào một cha (chạy trước khi dim dựng xong) | `InvocationMode.SUBPROCESS`, `source_rendering_behavior`, `should_detach_multiple_parents_tests` |
| 7 | User `airflow` không ghi được `/opt` | venv dbt ở `/opt/airflow/dbt-venv` |
| 8 | Audit `--wait 0` với dữ liệu vừa ghi báo `DIVERGED`: cửa sổ 5 phút chứa đơn chưa khép nên pipeline đúng khi chưa trích | Không phải lỗi, mà là đúng thiết kế ("chưa tới" mà hết hạn chờ là sự cố). Chạy lại DAG khi cửa sổ khép thì `CONVERGED` |

## 4. Kiểm chứng

- **Test:** 17 test mới: 11 tích hợp (`test_dbt_marts.py` 6 chạy dbt thật trên ClickHouse thật;
  pipeline 3; bộ giả lập năm tầng 2) + 6 test console. Số tổng và kết quả ruff/mypy/import-linter
  ở §6.
- **Kiểm đột biến 7/7:** bỏ khóa, nạp ngoài khóa, bảng rỗng không file mốc, chỉ tính lại tháng
  hiện tại, bỏ mép "đã nạp tới", bỏ phần dư chiết khấu, staging lấy mọi phiên bản. Cả 7 đều
  làm test đỏ.
- **Trên compose thật:**
  - 6 lượt DAG `success` (2 theo lịch, 4 kích tay), mỗi lượt ~2,5–3 phút. Không có dòng mới thì
    không phân vùng fact nào bị thay (AT-07).
  - Bộ giả lập `live-t0-3` (102 đơn / 55.316.000đ / 2.061 điểm) → DAG → audit **L0…L4
    `CONVERGED`**. Trễ từ lúc trung tâm ghi tới lúc vào mart = 1 cửa sổ + 1 lượt DAG, đúng thiết kế.
  - **`DROP DATABASE dw`** → một lượt DAG dựng lại toàn bộ từ lake → 13 con số (bronze, fact,
    dim) khớp tuyệt đối → audit cả 3 lần chạy vẫn `CONVERGED` L0…L4.
  - Đổi cửa sổ trích 1 giờ → 5 phút → 1 giờ giữa chừng: không chồng lấn, số không đổi.
  - **Spike S1:** 12 container `edge`+`central`+`data` dùng **2,8 GiB** lúc nghỉ, **đỉnh 3,9 GiB**
    giữa lượt DAG. **S3:** cosmos cho mỗi model một task.

## 5. Tiếp theo (còn lại của cổng A)

> Mục 1 xong cùng ngày: [2026-09-24-giai-doan-a-virtual.md](2026-09-24-giai-doan-a-virtual.md).

1. **Bộ giả lập chế độ `virtual` + tật `offline`, `resend`, `concurrent_customer`**
   ([18 §5](../18-simulator.md)), 20 cửa hàng → `CONVERGED`. Tật `offline` vắt qua cuối tháng
   kiểm lại bẫy 5 trên compose.
2. `t0` 3 cửa hàng chạy **2 giờ liên tục** → `CONVERGED` L0…L4 (cần thêm 2 cửa hàng vào compose).
3. AT-01…04 thành `tests/scenarios/`; AT-10 (đối soát INV-4 + `dbt test` xanh trong cùng một lượt).
4. Dashboard "sức khỏe luồng" ([17 §6](../17-data-flow.md)): độ tươi L2/L4, trễ theo chặng. Audit
   chưa báo độ tươi.
5. Nhỏ: CI chưa kiểm DAG đọc được (cần image Airflow); chưa có `fernet_key` (chưa lưu
   connection nào trong Airflow); inferred member của dữ liệu demo tuần 1 còn 2 cảnh báo.

## 6. Trạng thái cuối

- Toàn repo **383/383** test (366 → 383). ruff, `mypy --strict` (128 file), **12/12** hợp đồng
  import sạch. Link docs sạch.
- Compose đang chạy: `edge` + `central` + `data` (MinIO, ClickHouse, Airflow 3). DAG
  `retail_pipeline` bật theo lịch mặc định (phút 5 mỗi giờ). UI: http://localhost:8081.
- `infra/.env` được thêm `AIRFLOW_JWT_SECRET` (sinh ngẫu nhiên). Chưa commit gì.

---
*Viết bởi Claude (Opus 5.5) theo các lần kiểm chứng thật trong phiên 2026-09-24.*
