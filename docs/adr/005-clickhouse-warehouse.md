# ADR-005 — ClickHouse làm warehouse, MinIO + Parquet làm lake

**Trạng thái:** Được chấp nhận · 2026-09-11

## Bối cảnh

Cần một tầng phân tích trả lời được: doanh thu theo cửa hàng/thời gian, sản phẩm bán chạy,
phân bố hạng khách, hiệu quả khuyến mãi và chương trình loyalty.

Ràng buộc: chạy trên laptop, không tốn tiền, nhưng có đường lên production.

Khối lượng ([02](../02-scale-capacity.md)): T2 ~128 triệu dòng item/năm, T3 ~2 tỷ dòng/năm.

## Quyết định

**Kiến trúc hai tầng:**

1. **Lake** — MinIO (S3 API) chứa Parquet, phân vùng kiểu Hive, chia lớp bronze/silver
2. **Warehouse** — ClickHouse một node, star schema, lớp gold
3. **Biến đổi** — dbt-core

```
s3://lake/bronze/<nguồn>/<bảng>/dt=YYYY-MM-DD/store_id=<id>/*.parquet   (thô, bất biến)
s3://lake/silver/<thực thể>/dt=YYYY-MM-DD/*.parquet                      (sạch, khử trùng)
clickhouse://dw/{dim_*, fact_*}                                          (gold, star schema)
```

> 📝 **Đính chính 2026-09-23** (không đổi quyết định lake + ClickHouse + dbt, chỉ đổi bố cục).
> Chi tiết và lý do ở [17-data-flow §2](../17-data-flow.md):
> - Nguồn duy nhất là **Postgres trung tâm**, và **bỏ cấp `store_id=`**. Đường dẫn là
>   `s3://lake/bronze/central/<bảng>/dt=<ngày nạp>/<start>_<end>.parquet`. Ở T3, chia theo
>   cửa hàng sẽ sinh ~2000 file nhỏ mỗi cửa sổ trích xuất, vừa chậm đọc vừa không phục vụ
>   truy vấn nào (ClickHouse lọc theo `dt`, còn `store_id` là cột trong file).
> - **Silver trong POC là tầng staging của dbt bên trong ClickHouse**, không phải Parquet trên
>   MinIO. Silver dạng Parquet để v2, khi có consumer thứ hai ngoài ClickHouse.

## Vì sao cần lake, không nạp thẳng vào warehouse?

Đây là câu hỏi hợp lý — nó thêm một chặng. Ba lý do:

1. **Lưới an toàn.** Bronze bất biến nghĩa là warehouse **luôn dựng lại được từ đầu**. Khi
   phát hiện logic biến đổi sai (chuyện chắc chắn xảy ra), chỉ cần sửa dbt rồi chạy lại —
   không phải đi xin lại dữ liệu từ 200 cửa hàng.
2. **Tách rời.** Hệ thống vận hành không bị ràng buộc với schema warehouse. Đổi mô hình
   chiều không cần đụng vào OLTP.
3. **Rẻ.** Lưu trữ object rẻ hơn lưu trữ database một bậc độ lớn. Giữ được nhiều năm dữ
   liệu thô mà không phình warehouse.

Chi phí: thêm một chặng trong pipeline. Đáng.

## Vì sao ClickHouse

| Tiêu chí | ClickHouse |
|---|---|
| Khối lượng | Hàng tỷ dòng trên một node. T3 (2 tỷ dòng/năm) vẫn trong tầm |
| Tài nguyên local | ~1 GB RAM, chạy tốt trên laptop |
| Là server | Metabase kết nối trực tiếp, nhiều người dùng đồng thời |
| Nén | Cột + nén mạnh → dữ liệu retail nén rất tốt (nhiều giá trị lặp) |
| SQL | Phương ngữ gần chuẩn, học nhanh nếu đã biết SQL |
| dbt | `dbt-clickhouse` chín muồi |
| Đường lên cloud | ClickHouse Cloud, cùng SQL |
| Đọc Parquet trực tiếp | Đọc thẳng từ S3/MinIO bằng hàm `s3()` → nạp dữ liệu đơn giản |

Điểm cuối đáng chú ý: ClickHouse **đọc Parquet trên MinIO trực tiếp** bằng SQL. Việc nạp
lake → warehouse chỉ là một câu `INSERT INTO ... SELECT * FROM s3(...)`, không cần kéo dữ
liệu qua Python. Đây là lợi thế lớn về công sức.

## Phương án đã xem xét và loại

| Phương án | Ưu | Vì sao loại |
|---|---|---|
| **DuckDB** | Cực nhẹ, SQL tuyệt vời, không cần vận hành | **Nhúng trong tiến trình** — không phục vụ được BI nhiều người dùng, không có server để Metabase giữ kết nối. Sẽ phải di trú ở T2. *(Vẫn dùng DuckDB cho khám phá ad-hoc trên Parquet — công cụ tốt, chỉ không phải warehouse)* |
| **PostgreSQL làm luôn warehouse** | Không thêm hệ thống mới | Chạy được ở T1, sập ở T2+. Postgres là row-store, quét 128 Tr dòng để tổng hợp sẽ rất chậm. Chọn cái này = chấp nhận một cuộc di trú đã biết trước |
| **BigQuery / Snowflake** | Không phải vận hành, scale vô hạn | Không chạy local được (vi phạm ràng buộc), tốn tiền, khóa nhà cung cấp |
| **Iceberg + Trino** | Chuẩn lakehouse mở đầy đủ | Trino cần nhiều RAM, thêm hẳn một tầng khái niệm. Thừa cho POC 1 tháng |
| **DuckLake + DuckDB** *(xem xét 2026-09-11)* | Metadata nằm trong **Postgres** (ta đã có sẵn!) → tra metadata nhanh 10–100× so với Iceberg, ACID đa bảng, schema evolution, time travel, v1.0 production-ready từ 04/2026. Hồ sơ áp dụng của nó — "lakehouse local/nhúng, nhóm nhỏ dùng chung catalog Postgres" — **khớp gần như hoàn hảo với dự án này** | Vẫn vướng đúng vấn đề của DuckDB: **không có server cho BI**. Và hệ sinh thái ngoài DuckDB còn hẹp, mới ~5 tháng tuổi ở mức production. **Là ứng viên mạnh nhất cho v2** nếu Iceberg tỏ ra quá nặng — ghi vào backlog |
| **Druid / Pinot** | Realtime OLAP mạnh | Vận hành nặng hơn ClickHouse nhiều, mà không cần realtime ở v1 |
| **Chỉ Parquet + DuckDB, bỏ warehouse** | Đơn giản nhất | Không có nơi để BI kết nối ổn định, không có mô hình chiều được quản trị |

## Vì sao Parquet thuần chứ không Iceberg ngay

Iceberg là đích đến đúng: schema evolution, time travel, ghi đồng thời, hoán đổi engine.

Nhưng ở v1, Parquet + phân vùng Hive giải quyết 90% nhu cầu với 0 khái niệm mới. Ngân sách
1 tháng không cho phép học thêm một tầng metadata.

**Đường nâng cấp rẻ:** `pyiceberg` bọc được layout Parquet hiện có; chuyển đổi chủ yếu là
sinh metadata, **không phải viết lại dữ liệu**. Ghi vào backlog v2.

Kích hoạt khi: cần time travel, nhiều engine cùng ghi, hoặc schema thay đổi thường xuyên
gây đau.

## Vì sao dbt là bắt buộc, không phải tùy chọn

Với solo dev, dbt là công cụ có đòn bẩy cao nhất trong cả stack. Miễn phí ngay khi cài:

| Có sẵn từ dbt | Nếu tự viết bằng Python thì tốn |
|---|---|
| Mô hình incremental | Tự quản watermark, tự xử lý ghi đè phân vùng |
| Test dữ liệu (`not_null`, `unique`, `relationships`, `accepted_values`) | Tự viết framework kiểm tra |
| Đồ thị lineage | Tự vẽ, tự cập nhật |
| Tài liệu tự sinh | Tự viết |
| Môi trường dev/prod | Tự quản |
| Chiến lược materialization (view/table/incremental) | Tự cài |

FR-C06 (idempotent) và FR-C07 (kiểm tra chất lượng) gần như được dbt cho không. Đó là hai
trong số những lỗi nặng nhất của v1.

Phân lớp:
```
staging/      1-1 với nguồn, chỉ đổi tên và ép kiểu
intermediate/ logic nghiệp vụ, join
marts/        dim_* và fact_* — cái mà BI đụng vào
```

## Cơ chế idempotency trong ClickHouse *(cập nhật 2026-09-11 sau khi kiểm chứng)*

Phần này quan trọng vì FR-C06 là lỗi nặng nhất của v1. ClickHouse có **ba** cơ chế, và
chúng không tương đương — cần dùng đúng cái:

| Cơ chế | Cách hoạt động | Dùng khi nào |
|---|---|---|
| **`insert_deduplication_token`** ⭐ | Gán một token ổn định cho mỗi lô logic. Retry với cùng token → ClickHouse **bỏ qua hẳn lần insert đó**. Chính xác, không phụ thuộc merge | **Cơ chế chính.** Token = `<bảng>:<phân vùng ngày>:<store_id>` |
| **Thay trọn phân vùng** | `ALTER TABLE ... DROP PARTITION` rồi `INSERT` | Khi nạp lại (backfill) một ngày. Xác định hoàn toàn |
| **`ReplacingMergeTree`** | Khử trùng theo `ORDER BY` key ở thời điểm merge nền | **Lưới an toàn**, không phải cơ chế chính |

### ⚠️ Cạm bẫy phải biết về `ReplacingMergeTree`

`ReplacingMergeTree` chỉ cho **tính đúng đắn cuối cùng (eventual)** — khử trùng lặp diễn ra
trong merge nền, nên **một `SELECT` thường vẫn có thể trả về dòng trùng**. Muốn đọc dữ liệu
đã khử trùng phải dùng modifier `FINAL` (tốn kém hơn).

Hệ quả thiết kế: **không được dựa vào `ReplacingMergeTree` một mình cho FR-C06.** Nếu chỉ
dùng nó và test AT-07 bằng `SELECT count()` ngay sau khi nạp, test sẽ **đỏ giả** (hoặc tệ
hơn: xanh giả sau khi merge kịp chạy, rồi đỏ trên production).

Thiết kế chốt:
1. `insert_deduplication_token` cho mọi lần ghi — chống retry ở tầng vận chuyển
2. Thay trọn phân vùng khi nạp lại — chống chạy lại pipeline
3. `ReplacingMergeTree` làm lưới an toàn cuối
4. Test AT-07 đếm bằng `FINAL` **và** kiểm tra `system.parts` để không bị merge che mắt

> **Cập nhật 2026-09-24 (hiện thực S5, S6):** hai điểm của thiết kế trên đổi theo bằng chứng.
> (a) Token không phải `<bảng>:<ngày>:<store_id>` mà là **đường dẫn file + SHA-256 nội dung**:
> cùng token khác nội dung thì ClickHouse bỏ lần sau mà không báo gì
> ([17 §4 bẫy 4](../17-data-flow.md)). (b) **Mart không dùng `ReplacingMergeTree`**: fact được
> dựng lại trọn phân vùng tháng (`insert_overwrite`, cơ chế 2), nên lưới an toàn số 3 không
> còn việc gì để làm. Tệ hơn, nó sẽ **che** nhân đôi (sau merge, `count()` lại đúng) khỏi
> bộ đối soát L4, vốn phải đếm được nhân đôi. Có bộ đối soát rồi thì một lớp che còn tệ hơn
> không có gì. AT-07 kiểm bằng tên part trong `system.parts`: chạy lại không có dòng mới thì
> không part nào đổi. Bronze vẫn là `MergeTree` + token + cửa sổ khử trùng.

## Ghi chú thiết kế warehouse

Rút kinh nghiệm từ lỗi của v1:

| Lỗi v1 | Cách làm v2 |
|---|---|
| Bảng dimension không bao giờ được nạp | dbt model cho **mọi** dim, có test quan hệ khóa |
| `fact_sales.product_key String` vs `dim_product.product_key UInt32` | Kiểu surrogate key thống nhất, `relationships` test cưỡng chế |
| Nhét business key vào cột surrogate key | Tra cứu surrogate key rõ ràng trong tầng intermediate |
| `store_key` hardcode = 1 | `store_id` có mặt trong mọi bản ghi từ nguồn |
| Chạy lại là nhân đôi | Incremental theo phân vùng + `ReplacingMergeTree` |
| Fact ở mức đơn hàng, pivot wide rồi melt | Fact ở **mức dòng sản phẩm**, giữ dạng long xuyên suốt |
| Không có nguồn cho `fact_point_transaction` | `fact_point_event` lấy thẳng từ `point_ledger` — ledger vốn đã là bảng fact |

Điểm cuối đáng chú ý: **`point_ledger` chính là một fact table ở dạng vận hành.** Chọn mô
hình ledger ([ADR-002](002-point-ledger.md)) làm cho phần phân tích loyalty gần như miễn phí.

## Hệ quả

### Tích cực
- Warehouse luôn dựng lại được từ lake
- Chất lượng dữ liệu và idempotency gần như miễn phí nhờ dbt
- ClickHouse đọc Parquet trực tiếp → pipeline ít code
- Mọi thành phần đều có bản managed tương ứng
- Không cần di trú engine khi lên T2/T3

### Rủi ro nguồn cung — MinIO *(bổ sung 2026-09-24)*

Repo Docker Hub `minio/minio` **không còn tồn tại** (pull trả "repository does not exist",
phát hiện 2026-09-24 khi chạy test). MinIO chỉ còn phát hành image qua `quay.io/minio/minio`.
Compose và test đã chuyển sang `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z` và **ghim
phiên bản**, vì `latest` của một nguồn vừa đổi chính sách phát hành là thứ không nên tin.

Không đổi quyết định lake: pipeline chỉ nói giao thức S3 qua `pyarrow.fs.S3FileSystem` và hàm
`s3()` của ClickHouse, nên thay MinIO bằng một kho tương thích S3 khác (SeaweedFS, Garage, hoặc
S3/GCS thật ở production) là đổi image và endpoint, không đổi code. **Điều kiện xem lại:**
quay.io cũng ngừng phát hành, hoặc cần bản vá bảo mật mà không còn image.

### Tiêu cực
- Thêm một chặng (lake) so với nạp thẳng
- ClickHouse có những đặc thù riêng (engine table, `FINAL`, mutation đắt) — cần học
- Thêm ~1.3 GB RAM (ClickHouse + MinIO)
- Phải học dbt nếu chưa biết (~1 ngày, hoàn vốn ngay trong tuần đó)
