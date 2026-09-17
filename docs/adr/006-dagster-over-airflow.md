# ADR-006 — Dagster thay Airflow cho orchestration

**Trạng thái:** ❌ **Bị thay thế bởi [ADR-007](007-airflow-over-dagster.md)** · 2026-09-11
**Lý do thay thế:** chủ dự án đã có kinh nghiệm Airflow; và lập luận kỹ thuật chính của ADR
này (mô hình asset) đã mất giá trị khi Airflow 3.2 bổ sung asset partitioning (04/2026).

> Giữ lại ADR này vì phần phân tích tài nguyên vẫn đúng và vẫn cần thiết — nó là lý do
> [ADR-007](007-airflow-over-dagster.md) bắt buộc dùng **LocalExecutor** và **tách compose
> profile**.

## Bối cảnh

v1 dùng Airflow 2.10.5 với LocalExecutor: 4 container (webserver, scheduler, metadata DB,
init), khoảng 2.5–3 GB RAM. Hai DAG đã viết nhưng đều không idempotent và không có
watermark.

Cần một orchestrator cho: trích xuất tăng dần → nạp lake → chạy dbt → nạp warehouse →
kiểm tra chất lượng.

## Quyết định

Dùng **Dagster**.

## Lý do

### 1. Chi phí tài nguyên trên laptop *(lập luận mạnh nhất — đã kiểm chứng 2026-09-11)*

| | Container | RAM |
|---|---:|---:|
| **Airflow 3** (webserver, scheduler, triggerer, dag-processor, worker + Postgres + Redis) | **7** | Docs chính thức: **cấu hình Docker ≥ 4 GB, khuyến nghị 8 GB** |
| **Dagster** (webserver + daemon) | 1–2 | ~700 MB |

**Lưu ý quan trọng:** Airflow 3 **nặng hơn** Airflow 2, không nhẹ hơn. Nó tách `triggerer`
và `dag-processor` thành tiến trình riêng, nên số container tăng từ 4 lên 7. Tài liệu chính
thức của Airflow yêu cầu cấp cho Docker tối thiểu 4 GB và khuyến nghị 8 GB.

Đối chiếu với ngân sách trong [02 §5](../02-scale-capacity.md): tổng stack v2 dự kiến ~5 GB
trên máy 16 GB. **Riêng Airflow ở mức khuyến nghị đã ăn 8 GB** — cộng 3 cửa hàng mô phỏng
+ ClickHouse + MinIO thì vượt trần. Đây không phải chuyện tối ưu cho đẹp, mà là chuyện
**stack có chạy được trên máy của bạn hay không.**

### 2. Mô hình asset — khoảng cách đã hẹp lại đáng kể

Đây là chỗ tôi phải cập nhật cho trung thực. Lập luận ban đầu của tôi ("chỉ Dagster có asset
và partition hạng nhất") **không còn đúng**:

- Airflow **3.0** (04/2025) đã bổ sung asset-aware scheduling.
- Airflow **3.2** (07/04/2026) bổ sung **asset partitioning** — DAG hạ nguồn kích hoạt được
  theo *một phân vùng* của asset thượng nguồn, thay vì chờ toàn bộ dataset làm mới.
- Airflow 3.2 và 3.3 đã "đóng phần lớn khoảng cách với Dagster về phía data-aware".

Vậy lợi thế còn lại của Dagster ở mặt này là **mức độ chín và mượt của mô hình**, không phải
sự tồn tại của nó. Dagster sinh ra từ mô hình asset; Airflow gắn thêm vào. Với dự án
greenfield, dbt-centric, một người — khác biệt vẫn nghiêng về Dagster, nhưng **nhẹ hơn nhiều
so với lập luận tôi viết ban đầu**.

Đánh giá của ngành khớp với bối cảnh này: *Dagster phù hợp cho các dự án greenfield,
dbt-centric, một nhóm làm analytics — trong khi Airflow mạnh hơn cho nền tảng production
nhiều nhóm, cần độ rộng hệ sinh thái, độ chín về governance, và **nguồn tuyển dụng lớn nhất
trong lĩnh vực orchestration**.* Dự án này đúng là trường hợp đầu.

### 2b. Idempotency được khuyến khích bởi thiết kế

Với mô hình asset, "asset cho phân vùng ngày X" tự nhiên là **ghi đè**, không phải chèn
thêm — đúng vào lỗi nặng nhất của v1. Airflow 3.2 giờ cũng làm được, nhưng Dagster khiến
việc làm sai khó hơn.

### 3. Tích hợp dbt

`dagster-dbt` biến **mỗi dbt model thành một asset Dagster**, với lineage đầy đủ hiển thị
trong cùng một đồ thị với các asset ingest. Một màn hình duy nhất từ Postgres → lake →
dbt → ClickHouse.

Với Airflow, dbt thường là một `BashOperator` — một hộp đen trong DAG, mất hết lineage.

### 4. Vòng lặp phát triển

Dagster hot-reload định nghĩa asset. Airflow phải chờ scheduler quét file (mặc định 30s,
v1 đã phải chỉnh xuống 10s). Ở tuần thứ 3 khi đang lặp nhanh, khác biệt này cộng dồn đáng kể.

Ngoài ra Dagster chạy được **không cần Docker** khi phát triển (`dagster dev`) — test
nhanh hơn nhiều.

### 5. Nguyên nhân lỗi của v1 được xử lý bởi thiết kế

| Lỗi v1 | Dagster xử lý thế nào |
|---|---|
| `WHERE created_at <= NOW() - INTERVAL 7 DAY` lấy lại toàn bộ lịch sử mỗi lần | Partition đưa khoảng thời gian vào làm tham số. Không có chỗ để viết sai kiểu này |
| Chạy lại là nhân đôi dữ liệu | Ghi đè theo phân vùng là mặc định |
| `open('config/pos_config.json')` phụ thuộc cwd | Resource và config được inject, không đọc file theo đường dẫn tương đối |
| Không idempotent | Asset được mô hình hóa là "trạng thái", không phải "hành động" |

## Lập luận ngược — vì sao có thể vẫn nên chọn Airflow

Trình bày trung thực, vì đây là quyết định sát nút:

| Lợi thế của Airflow | Sức nặng |
|---|---|
| **Giá trị trên CV** | Airflow có **nguồn tuyển dụng lớn nhất** trong orchestration — xuất hiện trong tin tuyển dụng nhiều hơn Dagster đáng kể. Nếu dự án này để xin việc, đây là lập luận mạnh |
| **Asset model đã có** | Airflow 3.2 (04/2026) đã có asset partitioning. Lý do kỹ thuật để chọn Dagster đã yếu đi rõ rệt |
| **Đã quen** | Bạn đã viết 2 DAG. Chuyển sang Dagster tốn ~1 ngày học |
| **Hệ sinh thái provider** | Airflow có nhiều operator dựng sẵn hơn. Ít quan trọng ở đây vì chỉ dùng Postgres/S3/ClickHouse |
| **Cộng đồng** | Lớn hơn, dễ tìm câu trả lời hơn |

**Đánh giá:** với ràng buộc "POC cho doanh nghiệp thật + 1 người + 1 tháng + laptop", tiết
kiệm 2 GB RAM và vòng lặp phát triển nhanh hơn thắng giá trị CV. Nhưng nếu ưu tiên đổi
(VD: dự án chuyển thành portfolio xin việc), **đảo quyết định này là hợp lý** — chi phí
chuyển đổi thấp vì logic nghiệp vụ nằm trong dbt, không nằm trong orchestrator.

## Phương án đã xem xét và loại

| Phương án | Vì sao loại |
|---|---|
| **Airflow 3** | Xem trên. **Là lựa chọn thay thế hoàn toàn hợp lệ** — chỉ vướng đúng một điểm: 7 container và ngưỡng RAM khuyến nghị 8 GB không vừa ngân sách laptop khi chạy song song 3 cửa hàng mô phỏng. Nếu bạn chấp nhận chạy data platform **tách riêng** khỏi edge (bật/tắt bằng compose profile), Airflow 3 trở lại thành ứng viên ngang ngửa |
| **Prefect** | DX tốt, nhẹ. Nhưng mô hình asset/partition của Dagster khớp bài toán lakehouse hơn, và tích hợp dbt yếu hơn |
| **Cron + script Python** | Đủ cho v1 thật, nhưng không có backfill, không có UI, không có retry, không thấy lineage. Sẽ phải bỏ đi khi lớn hơn |
| **Chỉ dùng `dbt build` + cron** | Hấp dẫn — dbt lo phần biến đổi, cron lo lịch chạy. Nhưng phần **ingest** (Postgres → lake) vẫn cần điều phối, retry, phân vùng |
| **Mage / Kestra** | Cộng đồng nhỏ hơn, rủi ro cao hơn cho dự án có thể lên production |

## Hệ quả

### Tích cực
- Tiết kiệm ~2 GB RAM và 3 container
- Backfill theo khoảng ngày (FR-C09) gần như miễn phí
- Lineage thống nhất giữa ingest và dbt trên một đồ thị
- Idempotency được khuyến khích bởi thiết kế, không phải bởi kỷ luật
- Vòng lặp phát triển nhanh hơn rõ rệt

### Tiêu cực
- ~1 ngày học Dagster
- Ít phổ biến hơn Airflow → giá trị CV thấp hơn, ít tài liệu cộng đồng hơn
- Mô hình asset lạ với người quen tư duy DAG-task

### Giảm thiểu rủi ro
Giữ **toàn bộ logic nghiệp vụ trong dbt và trong hàm Python thuần**. Dagster chỉ làm việc
điều phối. Như vậy nếu đổi sang Airflow sau này, chỉ phải viết lại lớp vỏ điều phối —
ước tính 1–2 ngày, không phải viết lại pipeline.

**Đây là ADR dễ đảo nhất trong bộ này. Thiết kế để nó dễ đảo.**
