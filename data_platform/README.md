# Nền tảng dữ liệu (lớp C, T+1)

```
airflow/dags/retail_pipeline.py   DAG S4 → S5 → S6 — Airflow 3 LocalExecutor (ADR-007) + cosmos
dbt/                              staging (silver) → marts: dim_* + fact_* (ADR-005, docs/05 §7)
  macros/cutoff.sql               mép "đã nạp tới" + tập tháng bị ảnh hưởng (bẫy 5) — ĐỌC TRƯỚC
  macros/keys.sql                 khóa thay thế tất định (cityHash64), 0 = "không có"
  tests/                          cổng A (Σ thanh toán = Σ bán), bronze không nhân đôi, ...
requirements.txt                  môi trường Airflow (cài kèm constraint chính thức của Airflow)
requirements-dbt.txt              dbt — venv RIÊNG; một chỗ ghim bản cho image, Makefile và test
```

Chạy dbt từ máy dev: `make dbt-build` (uvx, không cài dbt vào môi trường của repo; cần
`CLICKHOUSE_PASSWORD`). Trong compose: `make up-data`, UI Airflow ở http://localhost:8081,
`make dag-run` để chạy ngay. Test S6 trên ClickHouse thật: `tests/integration/test_dbt_marts.py`.

Năm quy tắc khi thêm model (lý do ở [docs/17 §4 bẫy 5](../docs/17-data-flow.md) và
[macros/incremental.sql](dbt/macros/incremental.sql)):
1. Fact tăng dần = `insert_overwrite` + `affected_month_list()` + `pipeline_cutoff()`, không tự
   tính mép cắt.
2. **Một lượt phải tốn O(tháng bị ảnh hưởng), không O(lịch sử)** (2026-09-25): fact đọc THẲNG
   bronze qua `latest_sales()` / `month_filter()` / `child_dt_filter()` — điều kiện tháng đẩy xuống
   TRƯỚC khi khử trùng, phân vùng `_dt` cắt theo nhật ký nạp. KHÔNG `ref('stg_sale')` trong model
   chạy mỗi giờ: view đó khử trùng cả lịch sử (ở T2 là 79 triệu đơn), và ClickHouse không đẩy
   điều kiện qua `LIMIT BY`. Dim lấy tập khóa "đã thấy" bằng `DISTINCT` một cột ở bronze.
3. Staging của bảng incremental lọc `recorded_at < loaded_until()` (staging còn cho truy vấn tay
   và test, không cho fact).
4. Test trên fact lọc `occurred_at >= now64(6) - toIntervalDay({{ var('test_window_days', 35) }})`
   — DAG chạy test MỖI GIỜ. Toàn bộ lịch sử: `dbt test --vars '{test_window_days: 36500}'`.
   Dùng `now64`/`toDate32`: `now() - 36500 ngày` TRÀN kiểu `DateTime` (ra năm 2062) và test xanh mà
   không kiểm gì. Dựng lại một khoảng: `--vars '{rebuild_months: [202501, 202502]}'`.
5. ClickHouse cho alias nhìn thấy trong `WHERE`: `f(x) AS x ... WHERE x ...` là kiểm alias,
   không kiểm cột. Lọc trong subquery trước (đã gặp: NULL lọt qua thành UUID 0).

**Code trích xuất (S4) và nạp (S5) nằm ở [`packages/pipeline/`](../packages/pipeline/)**, không
ở thư mục này: nó được lint, `mypy --strict` và test như mọi code khác, và chạy được cả trong
task Airflow lẫn từ dòng lệnh (`python -m pipeline init|run`). DDL bronze ở
[`packages/pipeline/ddl/bronze.sql`](../packages/pipeline/ddl/bronze.sql). ĐỌC phần đầu file trước
khi thêm bảng (bẫy 4). Thư mục này giữ phần của Airflow và dbt.

Trích xuất viết bằng **Python thuần**, không dùng `dlt` (ADR-008 Q2). Nguồn **duy nhất** là
Postgres trung tâm, không bao giờ đọc Postgres cửa hàng. Silver trong POC là tầng staging của
dbt bên trong ClickHouse. Bản đồ 6 chặng và **5 bẫy phải xử lý trước khi viết code** ở
[docs/17-data-flow.md](../docs/17-data-flow.md). Đây là trọng tâm của giai đoạn A
([ADR-010](../docs/adr/010-data-flow-first.md)).

Bốn ràng buộc chi phối tầng này là #3 (bronze phân vùng theo `recorded_at`), #4
(`insert_deduplication_token`), #5 (`fact_payment` tách riêng) và #8 (đối soát tăng dần) —
đọc ở [CLAUDE.md](../CLAUDE.md) và [docs/05 §7](../docs/05-data-model.md).
