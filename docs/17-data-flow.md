# 17 — Luồng dữ liệu đầu-cuối

> Theo [ADR-010](adr/010-data-flow-first.md), luồng dữ liệu là **sản phẩm chính** của giai
> đoạn A và B. Doc này là bản đồ một trang cho toàn luồng: mỗi chặng nhận gì, ghi gì, lặp lại
> có an toàn không, đo bằng chỉ số nào, và thế nào thì coi là "ổn định".
>
> Không lặp lại chi tiết đã có ở chỗ khác. Chỉ ghi phần **nối** giữa các chặng, và ghi rõ
> những bẫy phát hiện được khi đọc lại schema (§4).

---

## 1. Toàn cảnh

```
 ┌─ CỬA HÀNG ─────────────────────────┐   ┌─ TRUNG TÂM ────────────────┐   ┌─ NỀN TẢNG DỮ LIỆU ───────────────────────────┐
 │                                    │   │                            │   │                                              │
 │ [Bộ giả lập] ─HTTP─► Edge API      │   │                            │   │                                              │
 │                      │ use case    │   │                            │   │                                              │
 │  S1  một transaction ▼             │   │  S3  một SAVEPOINT/sự kiện │   │  S4 trích xuất   S5 nạp         S6 biến đổi  │
 │  sale · line · payment · ledger ───┼─S2┼─► processed_event ─► replica ┼───┼─► bronze ──────► ClickHouse ──► dbt marts    │
 │  customer_local · shift · OUTBOX   │HTTP│   point_ledger · balance   │   │   Parquet        bronze_*       dim_* fact_* │
 │                                    │   │   customer · shift_replica │   │   dt=recorded_at  token dedupe  thay phân vùng│
 └────────────────────────────────────┘   └────────────────────────────┘   └──────────────────────────────────────────────┘
        khóa: event_id                        khóa: event_id                   khóa: (bảng, cửa sổ recorded_at) → đường dẫn file → token
```

**Nguồn trích xuất duy nhất là Postgres TRUNG TÂM.** Nền tảng dữ liệu không bao giờ đọc
Postgres cửa hàng, vì hai lý do đã có sẵn trong thiết kế:
(1) cửa hàng nằm sau NAT, trung tâm không có đường gọi vào ([14 §2](14-sequence-flows.md));
(2) bronze phân vùng theo `recorded_at` (ràng buộc #3), mà cột này chỉ tồn tại ở trung tâm.
Sơ đồ cũ ở [03 §2](03-architecture.md) vẽ mũi tên trích xuất từ Postgres cửa hàng. Đó là lỗi
của doc, đã sửa kèm changelog.

## 2. Từng chặng

| Chặng | Vào → Ra | Khóa idempotency | Lặp lại thì sao | Trạng thái |
|---|---|---|---|---|
| **S1** Ghi tại cửa hàng | Request `POST /sales`, `/customers`, `/shifts/*` → 5–7 bảng + `outbox`, **một transaction** | `sale_id` / `event_id` sinh tại cửa hàng | Transaction đã rollback thì không có gì; đã commit thì là giao dịch mới (client quyết) | ✅ `sales`, `customers`, `shifts/open`, `shifts/{id}/close` |
| **S2** Đẩy lên | `outbox` chưa gửi → `POST /events` theo lô | `event_id` | Vô hại: trung tâm trả lại `accepted` (AT-03) | ✅ |
| **S3** Áp ở trung tâm | Envelope → `processed_event` + bảng replica/ledger/balance/customer | `processed_event.event_id` | Vô hại: cổng idempotency chặn trong cùng SAVEPOINT | ✅ (trừ `SaleReturned`) |
| **S4** Trích xuất | Bảng trung tâm, cửa sổ `[mép cuối file cuối cùng, …)` trên `recorded_at`, mép cuối ≤ `extract_horizon()` → Parquet ở `s3://lake/bronze/central/<bảng>/dt=<ngày UTC của mép đầu>/<start>_<end>.parquet` | Đường dẫn file tất định theo cửa sổ | File đã có thì **không bao giờ trích lại** (lake bất biến, chính lake là trạng thái) | ✅ 2026-09-24 `packages/pipeline/extract.py` |
| **S5** Nạp ClickHouse | File bronze → bảng `bronze_<bảng>` qua `s3()`, ghi `bronze_load_log` | `insert_deduplication_token` = đường dẫn + SHA-256 nội dung | Nhật ký nạp bỏ qua file đã nạp; mất nhật ký thì token + hash nội dung chặn nhân đôi | ✅ 2026-09-24 `packages/pipeline/load.py` |
| **S6** Biến đổi | `bronze_*` → dbt staging (silver) → `dim_*`, `fact_*`, chỉ phần bronze dưới mép "đã nạp tới" | Phân vùng tháng theo `occurred_at` | `insert_overwrite` thay trọn đúng những tháng có dòng mới (bẫy 5); không có dòng mới thì không thay phân vùng nào | ✅ 2026-09-24 `data_platform/dbt/`, DAG `retail_pipeline` (Airflow 3 + cosmos) |

Chi tiết từng chặng: S1 [03 §4.1](03-architecture.md), [13 §3](13-api-contracts.md) · S2
[14 §4](14-sequence-flows.md) · S3 [13 §2](13-api-contracts.md), [12 §5](12-event-schema.md) ·
S4–S6 [05 §7](05-data-model.md), [ADR-005](adr/005-clickhouse-warehouse.md),
[ADR-008 C3/Q2](adr/008-remaining-decisions.md).

**Bronze không chia thư mục theo cửa hàng** (khác bản đầu của [ADR-005](adr/005-clickhouse-warehouse.md)).
Ở T3, mỗi cửa sổ trích xuất sẽ sinh ~2000 file nhỏ, trong khi không truy vấn nào cần cấp
thư mục đó: ClickHouse lọc theo `dt`, còn `store_id` đã là một cột trong file.

### Tầng silver nằm ở đâu

Trong POC, **silver là tầng staging của dbt bên trong ClickHouse**, không phải một bản
Parquet thứ hai trên MinIO. Lý do: một bản Parquet silver là thêm một lần ghi và thêm một chỗ
phải làm idempotency, trong khi không chứng minh thêm được điều gì. Khả năng "xóa warehouse,
dựng lại từ bronze" vẫn còn nguyên, vì bronze vẫn là bản bất biến.
Lộ trình cũ đã coi silver là thứ cắt đầu tiên nếu trễ ([06](06-roadmap.md)). Giờ đây nó là
lựa chọn mặc định. Silver dạng Parquet để lại cho v2, khi có consumer thứ hai ngoài ClickHouse.

### Bảng nào được trích xuất

| Bảng trung tâm | Cột watermark | Ghi chú |
|---|---|---|
| `sale_replica` | `recorded_at` | Index `sale_replica_recorded_idx`. Trigger giữ watermark khi có UPDATE (bẫy 3 ✅) |
| `sale_line_replica`, `sale_payment_replica` | — (join cha `sale_replica.recorded_at`) | Ghi trong cùng SAVEPOINT với cha, nên luôn cùng cửa sổ |
| `point_ledger` | `recorded_at` | Append-only. Index `point_ledger_recorded_idx` (bẫy 2 ✅) |
| `shift_replica` | `recorded_at` | Trigger giữ watermark kể cả khi sau này có UPDATE (bẫy 3 ✅) |
| `customer` | `recorded_at` | Cột + trigger thêm ở migration `0004` (bẫy 3 ✅). Không trích xuất cột PII |
| `point_balance` | `updated_at` | Hạng hiện tại cho `dim_customer`. Trigger + index (bẫy 3 ✅) |
| `store`, `product`, `employee`, `tier_rule` | Nạp toàn bộ mỗi lần (master data, nhỏ) | < 10 nghìn dòng. Toàn bộ bảng là một snapshot theo ngày |

## 3. Thời gian: ba đồng hồ

| Cột | Ai đóng dấu | Dùng để |
|---|---|---|
| `occurred_at` | Cửa hàng, lúc chốt đơn | Phân vùng `point_ledger` và fact, tính doanh thu theo ngày |
| `recorded_at` | Trung tâm, lúc áp sự kiện (`now()` của transaction ingest) | Phân vùng bronze, watermark trích xuất, đo trễ đồng bộ |
| `business_date` | Cửa hàng, theo ca | Báo cáo ngày kinh doanh (ca qua nửa đêm) |

**Độ trễ đồng bộ** của một sự kiện là `recorded_at - occurred_at`. **Độ tươi đầu-cuối** của
warehouse là `now() - max(occurred_at)` trong `fact_sale_line` của từng cửa hàng.

## 4. ⚠️ Năm bẫy thấy trước — trạng thái sửa

Tìm thấy khi đọc lại schema để viết doc này, **trước khi** viết code trích xuất. Đây là cùng
loại lỗi mà tuần 2 chỉ phát hiện ra khi dữ liệu đã chảy thật (docs/progress 2026-09-23 §2).
Mỗi bẫy làm dữ liệu lọt khỏi (hoặc nhân đôi trong) warehouse **mà không có lỗi nào**.

| # | Bẫy | Trạng thái | Sửa ở | Test (đã kiểm bằng đột biến) |
|---|---|---|---|---|
| 1 | Transaction commit muộn lọt khỏi watermark | ✅ 2026-09-23 | Hàm `extract_horizon()` + `transaction_timeout` của Central API | `test_extract_watermarks.py` |
| 2 | `point_ledger` không có index `recorded_at` | ✅ 2026-09-23 | Migration `0004` | `test_extract_watermarks.py` |
| 3 | Bảng bị UPDATE mà không chạm watermark | ✅ 2026-09-23 | Migration `0004`: cột + trigger | `test_extract_watermarks.py` |
| 4 | Token ClickHouse bị bỏ qua âm thầm | ✅ 2026-09-23 | [`packages/pipeline/ddl/bronze.sql`](../packages/pipeline/ddl/bronze.sql) | `test_clickhouse_bronze.py`, `test_pipeline.py` |
| 5 | Dữ liệu đến trễ phải tính lại đúng tháng | ✅ 2026-09-24 | `data_platform/dbt/macros/cutoff.sql` + 3 fact `insert_overwrite` | `test_dbt_marts.py` + compose (bộ giả lập `virtual`, offline qua 31/8) |

### Bẫy 1 — Transaction commit muộn làm lọt dòng khỏi watermark ✅

`recorded_at DEFAULT now()`, mà `now()` trong Postgres là **thời điểm BẮT ĐẦU transaction**,
không phải lúc commit. Mỗi lô ingest là một transaction, dài từ vài trăm ms tới vài giây với
lô 500 sự kiện. Tình huống lọt dòng:

```
10:00:59.8  lô ingest bắt đầu → mọi dòng mang recorded_at = 10:00:59.8
10:01:00.0  trích xuất cửa sổ [10:00, 10:01) chạy → chưa thấy các dòng đó (chưa commit)
10:01:01.5  lô commit
10:02:00.0  cửa sổ [10:01, 10:02) chạy → recorded_at = 10:00:59.8 < 10:01 → KHÔNG lấy
            ⇒ các dòng đó không bao giờ vào bronze
```

**Cách sửa (đã làm).** Bản thiết kế đầu định dùng một độ trễ an toàn cố định 5 phút. Bản
hiện thực **không đoán nữa, mà hỏi thẳng Postgres**. Hàm `extract_horizon(safety_lag)` trả về:

```
min( clock_timestamp() - safety_lag,  xact_start nhỏ nhất của các transaction CÒN MỞ )
```

Mọi dòng có `recorded_at` nhỏ hơn mốc này đều thuộc một transaction đã kết thúc, nên sẽ
không bao giờ còn dòng nào "đến sau" mà mang `recorded_at` nhỏ hơn. `safety_lag` (mặc định
30 giây) chỉ còn phải che một khe cỡ micro-giây, giữa lúc backend nhận câu lệnh và lúc nó báo
`xact_start`.

Quy tắc dùng hàm cho bộ trích xuất S4:
- Cửa sổ = `[mốc lần trước, extract_horizon())`. Tính mốc **trước**, rồi mới đọc dữ liệu
  bằng một câu lệnh **sau** đó. Mốc lần trước phải được lưu bền, chạy lại dùng lại đúng mốc đó.
- Một transaction treo làm trích xuất **đứng lại**, chứ không làm **mất** dòng. Đứng thì an
  toàn và thấy được qua chỉ số "cửa sổ trễ nhất" ở §6. `transaction_timeout = 60s` gắn vào mọi
  kết nối của Central API (`DB_TRANSACTION_TIMEOUT_SECONDS`) đặt trần cho thời gian đứng đó.
  `statement_timeout` không làm được việc này, vì nó chỉ chặn từng câu lệnh.
- **Role trích xuất cần `GRANT pg_read_all_stats`** nếu nó khác role ghi dữ liệu. Thiếu quyền
  thì `xact_start` của phiên khác là NULL, và `min()` sẽ lặng lẽ trả về một mốc SAI. Hàm
  **từ chối chạy** trong trường hợp đó. Bản đầu của lời chặn này bị chính test bắt lỗi: Postgres
  ẩn cả `backend_type` của phiên bị giấu, nên lọc theo cột đó trước khi đếm thì không đếm được gì.
- DI-4 (bronze vs Postgres theo từng ngày nạp) vẫn là lưới cuối cùng, chạy ở mỗi lần DAG.

### Bẫy 2 — `point_ledger` không có index trên `recorded_at` ✅

Bảng phân vùng theo `occurred_at`, trích xuất lại lọc theo `recorded_at`. Không có index thì
mỗi lần trích xuất **quét toàn bộ mọi partition**. Ở T3 (584 triệu dòng/năm) đây là đúng loại
quét toàn bộ mà ràng buộc #8 cấm cho job đối soát. Đã thêm `point_ledger_recorded_idx` trên
bảng cha. Postgres tự nhân index ra mọi partition, kể cả partition mà
`ensure_point_ledger_partitions()` tạo sau này (test kiểm cả hai trường hợp).

### Bẫy 3 — Bảng bị UPDATE mà không chạm cột watermark ✅

`CustomerUpdated` chạy `UPDATE customer SET ...` nhưng bảng chỉ có `joined_at`, không có cột
nào ghi lúc dòng đổi. Trích xuất tăng dần **không thấy được bản cập nhật**, nên `dim_customer`
sẽ giữ mãi giá trị cũ.

**Quy tắc cho mọi bảng thêm sau:** *bảng nào được trích xuất tăng dần thì mọi lần ghi vào nó
đều phải chạm cột watermark.* Quy tắc này được cưỡng chế bằng **trigger**, không bằng kỷ luật
ở từng handler. Trigger `BEFORE INSERT OR UPDATE` đặt `recorded_at := now()` trên `customer`,
`sale_replica`, `shift_replica`, và đặt `updated_at := now()` trên `point_balance`:
- Handler mới quên set cột (ví dụ `SaleVoided` sau này sửa `sale_replica.status`) → trigger
  vẫn set.
- Client cố khai giá trị khác → bị ghi đè. `recorded_at` là giờ trung tâm nhận, không ai được
  khai hộ.
- Phải là `now()`, **không** được là `clock_timestamp()`: `extract_horizon()` lập luận trên
  đúng mốc đầu transaction. Đổi hàm thời gian là mở lại bẫy 1.

Hệ quả cho tầng sau: một thực thể có thể có **nhiều phiên bản trong bronze** (mỗi lần UPDATE
lại được trích xuất). dbt staging phải lấy bản có `recorded_at` lớn nhất theo khóa nghiệp vụ.

### Bẫy 4 — `insert_deduplication_token` bị bỏ qua âm thầm trên MergeTree thường ✅

Khử trùng khi insert của ClickHouse bật sẵn cho bảng `Replicated*MergeTree`. Với MergeTree
một node, `non_replicated_deduplication_window` mặc định là **0**, nên token bị bỏ qua. Chạy lại
DAG sẽ nhân đôi dữ liệu mà không có lỗi nào. Đây đúng là lỗi v1 đã mắc.

**Đã kiểm trên đúng image của compose** (`clickhouse-server:24-alpine`, bản 24.10), không dựa
vào tài liệu. Kết quả được giữ lại thành test:

| Tình huống | Kết quả | Hệ quả cho bộ nạp |
|---|---|---|
| Không khai cửa sổ, insert cùng token hai lần | **Nhân đôi** | Mọi bảng `bronze_*` khai `non_replicated_deduplication_window = 100000`. Test đỏ nếu một bảng quên |
| Có cửa sổ, INSERT…SELECT nhiều khối, chạy lại | Không nhân đôi | Đường nạp bình thường an toàn |
| **Cùng token, nội dung khác** | Lần sau **bị bỏ âm thầm** | Token = đường dẫn file **+ SHA-256 nội dung**. Chỉ dùng đường dẫn thì sửa file rồi nạp lại sẽ mất bản sửa |
| Nạp lại token cũ hơn cửa sổ | **Nhân đôi** | Cửa sổ có hạn. Chạy lại quá xa phải đi đường dựng lại |
| `DROP PARTITION` rồi nạp lại cùng token | Vào được đủ | **Đường dựng lại:** xóa phân vùng tháng `_dt`, nạp lại các file của tháng đó |

✅ **Đã kiểm với MinIO thật (2026-09-24):** nạp `s3()` từ MinIO với `max_threads=1`, chạy lại
cùng file không nhân đôi (`test_pipeline.py`, và trên compose: chạy lại pipeline, số dòng giữ
nguyên).

**Phát hiện thêm khi kiểm đột biến:** bỏ token đi thì bronze VẪN không nhân đôi. Khi bảng có
cửa sổ khử trùng, ClickHouse còn khử trùng theo **hash nội dung khối**, và nội dung bronze của
ta tất định. Vậy với bảng dữ liệu, token là lớp bảo vệ thứ hai. Nhưng với bảng có cột
`DEFAULT now64()` (`bronze_load_log`), nội dung đổi sau mỗi lần ghi và **chỉ token** chặn được
nhân đôi. Test `test_content_hash_dedups_deterministic_blocks_but_only_token_saves_default_now`
giữ đúng phân biệt này. Hai lớp này không phải thừa: hash nội dung chỉ đúng khi nội dung tất
định, còn token thì luôn đúng.

### Bẫy 5 — Dữ liệu đến trễ phải kéo theo việc tính lại đúng tháng ✅

Cửa hàng offline 3 ngày, sau đó đồng bộ vào ngày 1 tháng sau. Khi đó bronze nhận các dòng
có `occurred_at` thuộc tháng trước. Model fact phân vùng theo tháng của `occurred_at` phải
**tính lại phân vùng tháng trước**, không chỉ tháng hiện tại. Quy tắc: mỗi lần chạy, tập
tháng cần thay = `SELECT DISTINCT toYYYYMM(occurred_at)` trên các dòng bronze mới trong cửa sổ.
Không được hardcode "tháng này".

✅ **Đã sửa (2026-09-24)** trong `data_platform/dbt/macros/cutoff.sql` + ba fact
(`materialized='incremental'`, `incremental_strategy='insert_overwrite'`, phân vùng
`toYYYYMM(occurred_at)`). Tập tháng = tháng của các dòng staging có `recorded_at` lớn hơn
`_recorded_at` lớn nhất đã có trong fact. Mỗi tháng trong tập được dựng lại TRỌN, và
`insert_overwrite` thay nguyên phân vùng. Test `test_late_sale_recomputes_the_month_it_belongs_to_not_the_current_one`:
đơn 23:30 ngày 31/8 tới trung tâm ngày 3/9, tháng 8 được tính lại, phân vùng tháng 9 **không
bị đụng** (cùng tên part). Đổi thành "chỉ tháng hiện tại" thì test đỏ.

✅ **Trên compose (2026-09-24), bằng bộ giả lập `virtual`:** 20 cửa hàng, 30/8–1/9, 5 cửa hàng
offline từ 18:00 ngày 31/8 tới 12:00 ngày 1/9. DAG lần 1 chạy giữa lúc offline: mart ngày 31/8
của cửa hàng `…-0005` có 164/308 đơn. Sau khi cửa hàng xả outbox, DAG lần 2 dựng lại phân vùng
`202608` → 308/308. Audit L0…L4 `CONVERGED` cho cả 20 cửa hàng (19.930 đơn).

Ba chi tiết chỉ lộ ra khi viết model thật:

1. **Mép cắt tính MỘT lần mỗi model** (`pipeline_cutoff()`, `run_query` rồi nhúng hằng). Fact
   dùng mép hai lần: tập tháng và dữ liệu. Nếu mỗi chỗ tự tính thì một lần nạp chen giữa cho
   hai mép khác nhau. Dòng nằm giữa hai mép thuộc tháng "không bị ảnh hưởng" bị bỏ qua
   **vĩnh viễn**, vì lần sau `_recorded_at` lớn nhất đã vượt qua chúng.
2. **Staging chỉ nhìn bronze dưới mép "đã nạp tới"** (min trên 7 bảng incremental, đòi đủ 7).
   Không có mép thì nạp chết giữa `sale_line` và `sale_payment` cho ra `fact_sale_line` có đơn
   mà `fact_payment` chưa có. Test cổng A "Σ thanh toán = Σ bán" khi đó đỏ **giả** và chặn cả
   luồng. Lúc đầu tôi tưởng thiếu mép thì mất dòng vĩnh viễn. Test chỉ ra là không: dòng nạp
   muộn vẫn có `recorded_at` lớn hơn mốc của fact. Thứ mép này giữ là **tính nhất quán giữa các
   fact**.
3. **Bảng rỗng phải có file mốc.** Mép "đã nạp tới" đòi đủ 7 bảng, mà `point_ledger` rỗng (chưa
   ai tích điểm) thì trước đây không sinh file nào. Mép khi đó tắc vĩnh viễn, còn audit L3 thì
   báo mọi đơn mất thật là "chưa tới". S4 giờ ghi một file rỗng cho cửa sổ ngay trước
   `floor(horizon)`. Làm vậy đúng vì dòng tương lai luôn có `recorded_at ≥ horizon`.

## 5. Đối soát xuyên tầng — định nghĩa "đúng và đủ"

Luồng dữ liệu chỉ được coi là đúng khi **cùng một con số** giữ nguyên qua mọi tầng. Đơn vị
so sánh là `(store_id, business_date)`:

| Tầng | Số đơn | Σ `total` | Σ `sale_payment.amount` | Σ `delta` điểm |
|---|---|---|---|---|
| L0 Bộ giả lập (manifest: đáp án) | ✔ | ✔ | ✔ | ✔ |
| L1 Postgres cửa hàng | ✔ | ✔ | ✔ | ✔ |
| L2 Postgres trung tâm (replica, ledger) | ✔ | ✔ | ✔ | ✔ |
| L3 Bronze (Parquet, đọc qua `s3()`) | ✔ | ✔ | ✔ | ✔ |
| L4 Marts (`fact_sale_line`, `fact_payment`, `fact_point_event`) | ✔ | ✔ (Σ `net_amount` = `line_total` − chiết khấu phân bổ) | ✔ | ✔ |

- **Mọi ô trong một cột phải bằng nhau.** Lệch ở đâu thì lỗi nằm ở chặng giữa hai tầng đó.
- Thêm theo khách: `point_balance.balance = SUM(point_ledger.delta)` (INV-4, DI-1) và bằng
  điểm kỳ vọng của khách trong manifest.
- L1 chỉ đọc được trong môi trường test, vì bộ đối soát có quyền vào DB cửa hàng. Ở
  production, trung tâm không đọc được cửa hàng. Tín hiệu thay thế là `outbox_depth` +
  `oldest_unsent_age` do cửa hàng tự báo, cộng `store_sync_status` ở trung tâm.
- ✅ **L0, L1, L2 đã có** (2026-09-23), **L3 và L4 đã có** (2026-09-24, `--clickhouse`,
  `--marts`). L4 đọc mart qua `dim_store`/`dim_shift`, đúng đường truy vấn của người phân
  tích, và so với L3 (lệch là lỗi S6, không lẫn S4/S5). Đơn thiếu ở mart mà `recorded_at` ≤
  `_recorded_at` lớn nhất của fact là mất. Trên compose: 3 lần chạy bộ giả lập, L0 = L1 = L2 =
  L3 = L4 theo từng ngày, kể cả sau khi **xóa sạch ClickHouse và dựng lại từ lake**. Ở L3, đơn thiếu mà `recorded_at` nằm dưới mép "đã nạp tới" (lấy từ
  `bronze_load_log`) là **mất**, nằm trên mép là **chưa tới lượt**. Cùng một phiên bản xuất hiện
  hai lần là khử trùng hỏng. Trên compose: 2 lần chạy bộ giả lập, L0 = L1 = L2 = L3 → `CONVERGED`.
  Đã chạy thật trên compose: 102 đơn, L0 = L1 = L2 từng con số, `CONVERGED`.
- Bộ đối soát là **một lệnh** (`simulator audit`, [18 §6](18-simulator.md)), chạy được bất
  cứ lúc nào, kể cả **giữa** test hỗn loạn. Khi luồng còn đang chảy, nó báo "chưa hội tụ" cùng
  độ trễ, chứ không báo "lệch".

## 6. Chỉ số theo chặng

✅ **Đã hiện thực (2026-09-25):** mọi chỉ số dưới đây là metric Prometheus thật, xem trên
dashboard **"Sức khỏe luồng dữ liệu"** (Grafana `http://localhost:3001`, thư mục `retail-sync`,
provision từ `infra/observability/grafana/dashboards/flow-health.json`). Tên ở cột "Metric" là
tên trong Prometheus.

| Chặng | Chỉ số | Metric | Ngưỡng | Ai phát |
|---|---|---|---|---|
| S1 | `sale_commit_duration` p95 | `http_server_request_duration_seconds{http_route="/api/v1/sales"}` | < 500 ms | edge-api (instrumentation FastAPI) |
| S2 | tuổi sự kiện chờ cũ nhất | `outbox_oldest_unsent_age_seconds{store_id}` | > 3600 cảnh báo | **edge-api** (không phải worker) |
| S2 | độ sâu outbox, dead-letter cửa hàng | `outbox_depth`, `outbox_dead_lettered` | > 5000; dead > 0 | edge-api |
| S2 | gửi được / lỗi đường truyền / bị từ chối | `sync_events_sent_total`, `sync_push_failures_total`, `sync_events_rejected_total{outcome}`, `sync_consecutive_failures` | lỗi đường truyền kéo dài | sync worker |
| S3 | `ingest_batch_duration` p95/p99 | `http_server_request_duration_seconds{http_route="/api/v1/events"}` | p95 < 1 s | central-api |
| S3 | kết cục từng sự kiện, lô bị từ chối | `ingest_events_total{outcome}` (accepted, duplicate, rejected_*), `ingest_batches_refused_total{reason}` | — | central-api |
| S3 | `store_sync_lag_seconds{store}`, lần cuối thấy cửa hàng | `store_sync_lag_seconds`, `store_last_seen_age_seconds` | > 900; > 3600 | flow-monitor |
| S3 | dead-letter trung tâm | `dead_letter_events{store_id}` | > 0 cảnh báo | flow-monitor |
| S4 | **cửa sổ trễ nhất chưa chạy**, transaction treo | `pipeline_extract_watermark_age_seconds{table}`, `pipeline_extract_horizon_lag_seconds` | > 2× chu kỳ; horizon ≫ `safety_lag` | flow-monitor |
| S5 | mép nạp từng bảng, mép "đã nạp tới" | `pipeline_load_watermark_age_seconds{table}`, `pipeline_loaded_until_age_seconds` | cách xa S4 = nạp chết giữa chừng | flow-monitor |
| S6 | đơn đã nạp mà fact chưa có | `pipeline_transform_pending_sales`, `pipeline_transform_lag_seconds` | > 1 chu kỳ DAG + 10 phút | flow-monitor |
| Toàn luồng | **độ tươi** `now() - max(occurred_at)` ở L2 và L4 | `flow_freshness_seconds{layer, store_id}` | L2 < 5 phút (mạng tốt) · L4 < 1 chu kỳ DAG + 10 phút | flow-monitor |
| Toàn luồng | **chênh đối soát** (§5) | `audit_status`, `audit_findings{level}`, `audit_behind_seconds{layer}` | **DIVERGED = sự cố nghiêm trọng nhất** | `simulator audit --otlp` |
| Toàn luồng | lệch INV-4 đang mở, job bảo trì còn chạy không | `reconcile_drift_count`, `reconcile_last_run_age_seconds` | > 0; > 3 giờ | flow-monitor |
| S3 (ràng buộc #2) | vùng đệm partition `point_ledger` | `point_ledger_partition_months_ahead` | < 2 tháng | flow-monitor |

Cảnh báo: 15 rule cùng ngưỡng ở `infra/observability/grafana/provisioning/alerting/retail-sync.yaml`
(test `test_flow_dashboard.py` đỏ nếu rule dùng metric không ai phát). Dashboard gom đúng các chỉ số này theo chiều trái → phải: một hàng ô số "phải về 0", rồi
S1 → S2 → S3 → S4–S6 → toàn luồng. Công cụ ở [10-observability](10-observability.md).

**Ba nguồn phát, chia theo loại chỉ số:**
- **Tần suất và độ trễ** do chính tiến trình của luồng tự phát (`shared.metrics`): edge-api,
  sync worker, central-api.
- **Trạng thái** (tuổi các mép, độ tươi, lệch đang mở) do **bộ giám sát luồng**
  (`python -m pipeline monitor`, service `flow-monitor`) đọc từ Postgres trung tâm, lake và
  ClickHouse mỗi 30 giây. Nó chỉ đọc, chạy ngoài DAG (DAG treo đúng là lúc các mép ngừng tiến),
  và nguồn nào chết thì `flow_monitor_source_up{source} = 0` còn chỉ số của nguồn đó **vắng
  mặt**, không báo 0 (0 trông như "không trễ"). `python -m pipeline health` in cùng các số đó
  một lần dạng JSON.
- **Chênh đối soát** chỉ bộ đối soát tính được (nó giữ đáp án): `simulator audit --otlp`, và
  `--watch 300` cho test ngâm/hỗn loạn.

**Hai loại "trễ", không lẫn nhau.** Các mép S4/S5/S6 đo trên `recorded_at` (đồng hồ trung tâm):
đúng cả với dữ liệu `virtual` có `occurred_at` trong quá khứ. Độ tươi L2/L4 đo trên
`occurred_at` theo định nghĩa §3, chỉ có nghĩa khi cửa hàng dùng đồng hồ thật (chế độ `edge`,
production). Với `virtual`, dùng `audit_behind_seconds` (tụt sau tầng tươi nhất).

Ngoài giờ bán, độ tươi và "lần cuối thấy cửa hàng" tăng dần là bình thường (cửa hàng đóng cửa
22:00). Ngưỡng cảnh báo (roadmap ngày 19) phải tính tới giờ mở cửa, không đặt ngưỡng phẳng.

## 7. "Ổn định" nghĩa là gì — tiêu chí đo được

Luồng được coi là ổn định khi **đồng thời** thỏa cả năm điều sau, dưới tải của bộ giả lập:

1. **Không mất:** chênh đối soát = 0 ở mọi tầng sau khi hội tụ, kể cả sau mọi kịch bản CH-1…7.
2. **Không nhân đôi:** chạy lại bất kỳ chặng nào (gửi lại lô, chạy lại DAG, backfill), mọi số
   ở §5 vẫn giữ nguyên (AT-03, AT-07, CH-7).
3. **Tự hồi phục:** sau mỗi lỗi có chủ đích, luồng tự hội tụ lại, không cần người can thiệp,
   trong thời gian có giới hạn (CH-1: < 5 phút sau khi mạng có lại).
4. **Trễ có trần:** độ tươi L2 và L4 nằm dưới ngưỡng ở §6 trong suốt test ngâm 72h. Trễ không
   được trôi dần lên.
5. **Tài nguyên phẳng:** RSS của app, số kết nối DB, kích thước `outbox` và bloat của nó
   không tăng theo thời gian khi tải không đổi (test ngâm).

---
*Changelog: 2026-09-25 — §6 hiện thực: bảng chỉ số kèm tên metric thật và nơi phát; bộ giám sát
luồng `pipeline.monitor`; dashboard Grafana provision từ repo; audit báo độ tươi và đẩy metric.*

*Changelog: 2026-09-24 (lần 3) — bẫy 5 kiểm lại trên compose bằng bộ giả lập `virtual`; hai phát
hiện mới ở [progress](progress/2026-09-24-giai-doan-a-virtual.md): partition tháng trước ở tháng
go-live, và ca đang mở chưa có ở trung tâm (dim_shift inferred).*

*Changelog: 2026-09-24 (lần 2) — S6 hiện thực (dbt, `data_platform/dbt/`) + DAG Airflow 3;
bẫy 5 ✅ kèm ba chi tiết (mép cắt tính một lần, mép "đã nạp tới" giữ nhất quán giữa các fact,
file mốc cho bảng rỗng); L4 cột tổng đổi sang `net_amount`; audit L4. S4 thêm advisory lock
(một lượt mỗi lúc).*

*Changelog: 2026-09-24 — S4/S5 hiện thực ở `packages/pipeline/` (lake bất biến là trạng thái,
cửa sổ nối tiếp nhau từ mép cuối file cuối, nhật ký nạp); DDL bronze chuyển về
`packages/pipeline/ddl/`; bẫy 4 thêm phát hiện "hash nội dung vs token"; audit L3.*

*Changelog: 2026-09-23 (lần 2) — sửa bẫy 1–4 bằng code + test (đã kiểm đột biến). Bẫy 1 đổi
cách chữa từ "độ trễ an toàn cố định 5 phút" sang `extract_horizon()` (mốc từ transaction đang
mở). Bẫy 4 thêm ba hệ quả rút ra khi kiểm trên ClickHouse thật: token phải chứa hash nội dung,
cửa sổ có hạn, đường dựng lại là DROP PARTITION. Bẫy 5 chờ model dbt.*

*Changelog: 2026-09-23 — tạo mới theo [ADR-010](adr/010-data-flow-first.md). Kèm 5 bẫy phát
hiện khi đọc lại schema trung tâm (chưa sửa code; thuộc giai đoạn A).*
