# ADR-007 — Airflow 3 làm orchestrator

**Trạng thái:** Được chấp nhận · 2026-09-11 · **Quyết định của chủ dự án**
**Thay thế:** [ADR-006](006-dagster-over-airflow.md) (Dagster)

## Bối cảnh

[ADR-006](006-dagster-over-airflow.md) đề xuất Dagster, chủ yếu vì tài nguyên (Airflow 3 =
7 container, docs khuyến nghị 8 GB; Dagster = 1–2 container, ~700 MB).

Sau khi kiểm chứng, lập luận kỹ thuật của ADR-006 đã **yếu đi đáng kể**: Airflow 3.0 (04/2025)
có asset-aware scheduling và **Airflow 3.2 (07/04/2026) có asset partitioning** — đóng phần
lớn khoảng cách data-aware vốn là lý do chính chọn Dagster.

## Quyết định

Dùng **Apache Airflow 3** với **LocalExecutor**.

Lý do: **chủ dự án đã có kinh nghiệm Airflow** (đã viết 2 DAG ở v1).

## Vì sao đây là lý do hợp lệ

Chủ dự án nêu nguyên tắc chọn stack là *"ưu tiên độ phù hợp cho hệ thống thực tế, không chạy
theo độ phổ biến"* — và chọn Airflow **không** mâu thuẫn với nguyên tắc đó:

1. **Kinh nghiệm sẵn có là một ràng buộc kỹ thuật thật, không phải sở thích.** Ngân sách là
   1 người / 1 tháng. Một ngày học Dagster cộng với rủi ro gặp lỗi lạ trong lúc gấp là chi
   phí thật. Với ngân sách này, "công cụ tôi gỡ lỗi được lúc 11 giờ đêm" có giá trị vận hành
   cụ thể.
2. **Khoảng cách kỹ thuật đã gần như khép lại.** Nếu Dagster vẫn là công cụ duy nhất có
   asset partitioning thì đánh đổi sẽ khác. Giờ thì không.
3. **Chi phí duy nhất là tài nguyên, và có cách xử lý** (xem dưới).
4. Airflow là dự án Apache, chín, độ rộng hệ sinh thái lớn — phù hợp với một POC có khả năng
   lên production thật.

## Xử lý vấn đề tài nguyên

Đây là chi phí duy nhất của quyết định này, và phải xử lý chủ động.

### 1. Dùng LocalExecutor, không dùng CeleryExecutor

| Executor | Container | Ước tính RAM |
|---|---:|---:|
| CeleryExecutor | 7 (web, scheduler, triggerer, dag-processor, worker, Postgres-meta, Redis) | ~2.5–3 GB |
| **LocalExecutor** ⭐ | **5** (web, scheduler, triggerer, dag-processor, Postgres-meta) | **~1.8–2.2 GB** |

Bỏ Celery worker và Redis. Với khối lượng pipeline của dự án (vài chục task/ngày),
LocalExecutor thừa sức. Tiết kiệm ~2 container và ~800 MB.

### 2. Tách profile — không chạy đồng thời edge và data platform

Đây là biện pháp quan trọng nhất. `compose.yaml` dùng profile:

```bash
docker compose --profile edge --profile central up     # phát triển POS/Loyalty
docker compose --profile data up                        # chạy pipeline
```

Chỉ khi chạy **kịch bản nghiệm thu đầy đủ** (tuần 4) mới cần bật tất cả cùng lúc — và lúc đó
giảm số cửa hàng mô phỏng từ 3 xuống 2.

### 3. Ngân sách RAM cập nhật

| Kịch bản | Thành phần | RAM |
|---|---|---:|
| Phát triển edge | 3 cửa hàng (PG+Redis+API) + trung tâm | ~2.9 GB |
| Phát triển data | Trung tâm + MinIO + ClickHouse + Airflow | ~4.2 GB |
| **Nghiệm thu đầy đủ** | 2 cửa hàng + trung tâm + data + Metabase | **~7 GB** |

Vẫn vừa máy 16 GB. So với ~5 GB nếu dùng Dagster — chênh ~2 GB, chấp nhận được khi đã tách
profile.

## Ràng buộc thiết kế bắt buộc *(để giữ khả năng đảo ngược)*

Quyết định này chỉ an toàn nếu Airflow được giữ ở vai trò **lớp vỏ điều phối mỏng**:

1. **Toàn bộ logic biến đổi nằm trong dbt.** Không viết logic nghiệp vụ trong DAG.
2. **Logic Python là hàm thuần, test được không cần Airflow.** DAG chỉ gọi hàm.
3. **Không đọc file theo đường dẫn tương đối trong DAG** — lỗi `open('config/pos_config.json')`
   của v1. Dùng Airflow Variables/Connections hoặc biến môi trường.
4. **Dùng `@asset` / asset-aware scheduling của Airflow 3**, không dùng tư duy task thuần —
   để giữ tính idempotent theo phân vùng.

Nếu giữ được bốn ràng buộc này, đổi sang Dagster sau ước tính 1–2 ngày.

## Bài học từ v1 phải tránh *(quan trọng — v1 dùng Airflow và sai hết)*

| Lỗi v1 | Cách làm đúng với Airflow 3 |
|---|---|
| `WHERE created_at <= NOW() - INTERVAL 7 DAY` chạy `@weekly` → lấy lại toàn bộ lịch sử | Dùng `data_interval_start` / `data_interval_end` làm tham số truy vấn. **Không bao giờ dùng `NOW()` trong SQL trích xuất** |
| Chạy lại là nhân đôi | Ghi đè theo phân vùng + `insert_deduplication_token` của ClickHouse ([ADR-005](005-clickhouse-warehouse.md)) |
| `open('config/pos_config.json')` phụ thuộc cwd | Airflow Connections cho từng cửa hàng |
| Hai cửa hàng dùng chung `mysql_conn_id` | Mỗi cửa hàng một Connection riêng |
| dbt là `BashOperator` hộp đen | Dùng `astronomer-cosmos` để render mỗi dbt model thành một task, giữ lineage |
| Không test | `pytest` cho hàm thuần + `dbt test` trong DAG |

Ghi chú: **`astronomer-cosmos`** là mảnh ghép quan trọng — nó cho Airflow khả năng hiển thị
lineage dbt tương đương `dagster-dbt`, bù lại lợi thế lớn nhất còn sót của Dagster.

## Hệ quả

### Tích cực
- Không mất ngày nào để học công cụ mới — dồn thẳng vào nghiệp vụ
- Chủ dự án tự gỡ lỗi được khi gặp sự cố
- Hệ sinh thái lớn, nhiều tài liệu, nhiều ví dụ
- Giá trị chuyển giao cao nếu dự án lên production thật

### Tiêu cực
- **+2 GB RAM** so với Dagster → bắt buộc tách compose profile
- Nhiều container hơn → khởi động chậm hơn
- Mô hình asset của Airflow mới hơn, ít ví dụ hơn Dagster → cẩn thận khi tìm tài liệu, nhiều
  bài viết cũ vẫn theo tư duy task thuần của Airflow 2
- **Rủi ro lặp lại lỗi v1** — v1 đã dùng Airflow và cả hai DAG đều không idempotent. Bảng
  "bài học" ở trên là bắt buộc đọc lại khi viết DAG đầu tiên

## Điều kiện xem lại

Quay lại Dagster nếu: RAM trở thành nút thắt thật dù đã tách profile, hoặc mô hình asset của
Airflow 3 tỏ ra vướng víu trong thực tế khi làm tuần 3.

## Hiện thực (2026-09-24)

Quyết định giữ nguyên; ghi lại những gì chỉ biết được khi dựng thật (chi tiết ở
[progress/2026-09-24-giai-doan-a-dbt-airflow.md](../progress/2026-09-24-giai-doan-a-dbt-airflow.md)):

- **Airflow 3.3.2**, 3 tiến trình: `api-server` (thay `webserver` đã bị bỏ), `scheduler`,
  `dag-processor` (bắt buộc ở Airflow 3). Không `triggerer` vì chưa có deferrable operator. Mọi
  thành phần phải dùng CHUNG `jwt_secret` + `execution_api_server_url`, thiếu thì mọi task chết
  với "Signature verification failed".
- **RAM đo thật:** api-server ~310 MB, dag-processor ~250 MB, scheduler 1,2 GB lúc nghỉ / 2,1 GB
  khi các task dbt chạy song song. Cả stack 3 cửa hàng + trung tâm + data platform: ~3,4 GiB, đỉnh
  3,9 GiB. Điều kiện "RAM thành nút thắt" ở dưới **chưa chạm**.
- Cosmos 1.15 cần 3 cấu hình, nếu không sẽ lặng lẽ sai: `InvocationMode.SUBPROCESS` (dbt ở venv
  riêng), `source_rendering_behavior` (không thì test trên source bị bỏ),
  `should_detach_multiple_parents_tests` (không thì test fact → dim chạy trước khi dim dựng xong).
- Bảng bài học v1 ở trên: dòng "Airflow Connections cho từng cửa hàng" **không còn áp dụng**. Pipeline
  chỉ đọc Postgres TRUNG TÂM (không bao giờ đọc cửa hàng, [17 §2](../17-data-flow.md)), cấu hình qua
  biến môi trường cùng tên với CLI `python -m pipeline`. DAG không chứa logic: `catchup=False` vì
  lake là trạng thái, `max_active_runs=1` + advisory lock chống hai lượt chồng nhau.
- Mô hình asset của Airflow 3 chưa dùng; DAG theo lịch (`PIPELINE_SCHEDULE`) là đủ ở giai đoạn A.

*Changelog: 2026-09-24 — thêm mục "Hiện thực".*

