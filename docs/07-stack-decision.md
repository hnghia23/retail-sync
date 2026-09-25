# 07 — Bảng chốt Tech Stack

> **Mục đích:** để bạn đánh giá và chốt. Mỗi dòng ghi **lý do chọn**, **phương án đã loại**,
> **độ tự tin** của tôi, và **chi phí đảo ngược** nếu sau này thấy sai.
>
> Trạng thái: ✅ **ĐÃ CHỐT TOÀN BỘ** · 2026-09-11. Kiểm chứng thực nghiệm sẽ làm ở spike
> ngày 0 ([06-roadmap](06-roadmap.md)) — chốt kịch bản trước theo yêu cầu của chủ dự án.

> 🔀 **2026-09-23 — [ADR-010](adr/010-data-flow-first.md) không đổi stack**, chỉ đổi thứ tự làm
> (luồng dữ liệu trước, tính năng sau). Hai điều chỉnh công cụ nhỏ đi kèm: test tải dùng **bộ
> giả lập** thay `k6` ([18 §9](18-simulator.md)); silver trong POC là **staging dbt trong
> ClickHouse**, không phải Parquet thứ hai ([17 §2](17-data-flow.md)). HTMX + Alpine vẫn là lựa
> chọn cho UI, chỉ là UI mới dời sang giai đoạn C.

## Cách đọc

| Ký hiệu | Nghĩa |
|---|---|
| 🟢 **Tự tin cao** | Có số liệu hoặc lý do kỹ thuật rõ ràng. Đảo lại cần lý do mạnh |
| 🟡 **Tự tin vừa** | Lựa chọn hợp lý nhưng phương án khác cũng ổn. Sẵn sàng đổi nếu bạn có ưu tiên khác |
| 🔴 **Sát nút** | Tôi nghiêng về một bên nhưng khác biệt nhỏ. **Ý kiến của bạn nên thắng** |
| 🔁 | Chi phí đảo ngược sau khi đã code |

---

## Nhóm A — Quyết định nền tảng (khó đảo, cần chốt kỹ)

### A1. PostgreSQL 18 cho mọi OLTP 🟢

**Chọn:** PostgreSQL 18 ở cả cửa hàng và trung tâm. Bỏ MySQL, bỏ Cassandra.

**Lý do — theo thứ tự sức nặng:**

1. **Số liệu:** tải ghi đỉnh ở 2000 cửa hàng là **~67 writes/giây** (~200/s khi bùng nổ).
   Một Postgres node làm 5.000–10.000 w/s → **dư 25–100×**. Cassandra chỉ cần ở khoảng
   200.000 cửa hàng, gấp 100 lần mục tiêu lớn nhất.
2. **Điểm tích lũy là dữ liệu tài chính.** Cassandra không có transaction đa dòng; ledger
   cần transaction để ghi "đơn hàng + dòng điểm + outbox" nguyên tử. Đây là lý do *đúng đắn*,
   không phải lý do *hiệu năng*.
3. **PG 18 có `uuidv7()` native** (RFC 9562, không cần extension) — đúng cái dự án cần cho
   `sale_id`/`event_id`. Benchmark 50 triệu dòng: insert ~1.8 phút vs ~20 phút của UUIDv4,
   index nhỏ hơn ~25%, range scan nhanh ~3×. Giải quyết đúng lỗi đụng ID của v1.
4. **Transactional DDL** — migration Alembic thất bại giữa chừng sẽ rollback trọn vẹn.
   MySQL gây implicit commit ở DDL, để lại schema nửa vời phải sửa tay. Với schema của một
   **sổ cái tài chính**, đây là khác biệt thật. *(MySQL 9.0 có atomic DDL — không phải
   transactional DDL.)*
5. **Một engine = một phương ngữ SQL, một công cụ migration, một chiến lược backup.** Với
   solo dev 1 tháng, đây là lý do nặng nhất trong thực tế. Xem [§B0](#b0-db-tại-cửa-hàng-postgres--mysql--sqlite--đang-xem-xét-lại).

**Đã loại:** Cassandra (không có con số nào biện minh) · MongoDB (không giải bài toán nào ta
có) · CockroachDB (đích đến, không phải điểm xuất phát)

**Còn để ngỏ ở tầng cửa hàng:** MySQL và SQLite — xem [§B0](#b0-db-tại-cửa-hàng-postgres--mysql--sqlite--đang-xem-xét-lại).
*(Lưu ý: DB **trung tâm** là Postgres thì đã chốt, không phụ thuộc kết quả B0.)*

**🔁 Đảo ngược:** Rất đắt. Đây là quyết định nền. → [ADR-001](adr/001-postgres-everywhere.md)

---

### A2. Điểm tích lũy dùng ledger append-only 🟢

**Chọn:** `point_ledger` chỉ ghi thêm. Số dư là `SUM(delta)`, có snapshot để đọc nhanh.
Không bao giờ `UPDATE` số dư.

**Lý do:** đây là **lỗi kiến trúc của v1**, không phải bug. Với mô hình số dư:

```
t0  Cửa hàng A đọc 100    t0  Cửa hàng B đọc 100
t1  A ghi 100+50 = 150    t2  B ghi 100+30 = 130   ← đè lên A, mất 50 điểm
```

Ledger biến bài toán đồng thuận phân tán thành **một phép cộng có tính giao hoán**: hai cửa
hàng ghi hai *dòng* khác nhau, `SUM` luôn đúng bất kể thứ tự đến hay đến trễ. Kèm theo, gần
như miễn phí: kiểm toán đầy đủ, idempotency, hoàn điểm khi trả hàng, điểm hết hạn, gộp khách
trùng — và **`point_ledger` chính là `fact_point_event`** cho warehouse.

**Đã loại:** số dư + khóa bi quan (cần khóa xuyên mạng → chết tính offline) · optimistic
locking (không hòa giải được cửa hàng ↔ trung tâm) · CRDT counter (phức tạp hơn mà không
thêm gì — ledger *là* một G-Counter dễ hiểu) · Cassandra counter (không đọc chính xác, không
kiểm toán)

**Đánh đổi đã chấp nhận:** số dư *eventual*, hội tụ < 5 phút. Khách mua 2 nơi trong vài phút
có thể thấy số cũ. **Điểm không bao giờ mất.** Mất điểm là không chấp nhận được, thấy trễ thì
chấp nhận được.

**Chi phí:** bảng lớn hơn nhiều (1 dòng/giao dịch thay vì 1 dòng/khách) → **bắt buộc phân
vùng theo tháng từ đầu**. Và **job đối soát là bắt buộc**, không tùy chọn.

**🔁 Đảo ngược:** Rất đắt, và gần như không nên đảo. → [ADR-002](adr/002-point-ledger.md)

---

### A3. Transactional Outbox + HTTP (chưa dùng Kafka) 🟢

**Chọn:** ghi outbox trong cùng transaction với dữ liệu nghiệp vụ; worker riêng đọc bằng
`FOR UPDATE SKIP LOCKED` và POST lô lên trung tâm; trung tâm dedupe theo `event_id`.

**Lý do:**

1. **Kafka không giải quyết vấn đề gốc.** v1 làm `session.commit()` rồi `publish_event()` —
   app chết ở giữa là **mất event vĩnh viễn**. Đó là *dual-write problem*, và Kafka không
   sửa được. Outbox mới là nền; Kafka chỉ là phương tiện vận chuyển.
2. **Sai mô hình triển khai ở edge.** Kafka ở cửa hàng = mỗi cửa hàng một broker (nặng),
   hoặc giữ kết nối bền tới Kafka trung tâm — mà chính kết nối đó là thứ hay đứt. Outbox
   trên Postgres cục bộ đã bền sẵn.
3. **Chưa có tải để biện minh:** T2 (200 cửa hàng) ≈ **4 events/giây**. Kafka làm hàng triệu.
4. **Chi phí:** ~2 GB RAM + ~1 tuần công = 25% ngân sách thời gian.

**Điểm quan trọng — outbox là *on-ramp*, không phải đường cùn:**

```
v1:  App → bảng outbox → worker HTTP → API trung tâm
                ↓ (đổi chỗ này, KHÔNG đổi application code)
v2:  App → bảng outbox → Debezium CDC → Redpanda → consumer
```

Nếu publish thẳng Kafka từ app, sau này muốn đổi gì cũng phải sửa app. Chọn outbox là chọn
đúng thứ tự.

**Khi nâng cấp:** > ~100 cửa hàng, hoặc worker không kịp xả outbox sau sự cố, hoặc cần
nhiều consumer. Dùng **Redpanda** thay Kafka: đã kiểm chứng — **ít tài nguyên hơn Kafka
3–6×**, một binary duy nhất, không cần ZooKeeper, cùng Kafka API.

**Đã loại:** publish thẳng Kafka (dual-write) · Postgres logical replication (ghép chặt
schema hai bên, không tiến hóa độc lập) · trung tâm chủ động kéo (cửa hàng nằm sau NAT) ·
RabbitMQ/NATS (vẫn dual-write nếu không có outbox)

**Việc bắt buộc:** job dọn outbox · bảng dead-letter · metrics độ sâu outbox + tuổi event
cũ nhất.

**🔁 Đảo ngược:** Rẻ — chỉ đổi cái đọc bảng outbox. → [ADR-003](adr/003-outbox-not-kafka.md)

---

### A4. Modular monolith ở edge 🟢

**Chọn:** một tiến trình FastAPI chứa module `pos` + `loyalty`, ranh giới cưỡng chế bằng
`import-linter` trong CI. Sync worker là tiến trình riêng.

**Lý do:** v1 đã thử tách 2 service và **thất bại đúng theo cách này** —
`loyalty_api.py` rỗng 0 byte, `place_order.py` chỉ có `# TODO`. Chi phí phối hợp tiêu hết
ngân sách trước khi tính năng kịp xong.

Ở cửa hàng, hai module **luôn deploy cùng nhau, scale cùng nhau, chết cùng nhau** (cùng một
máy). Microservices trả lời "làm sao nhiều đội làm việc độc lập" và "làm sao scale riêng
từng phần" — dự án này **một người**, và hai module cùng hồ sơ tải. **Không có lợi ích vận
hành nào.**

Đổi lại, monolith cho một thứ rất giá trị: **"lưu đơn + tích điểm" nằm trong một transaction
ACID** → đúng đắn miễn phí, không cần saga.

**Nhưng giữ ranh giới nghiêm ngặt**, vì đó chính là đường cắt tương lai. Cưỡng chế bằng
công cụ, **không dựa vào kỷ luật** — kỷ luật sẽ trượt lúc 11 giờ đêm tuần thứ 3.

**🔁 Đảo ngược:** Rẻ *nếu* ranh giới được giữ. Đắt nếu để rò rỉ. → [ADR-004](adr/004-modular-monolith.md)

---

## Nhóm B — Lựa chọn công cụ (dễ đảo hơn)

### B0. DB tại cửa hàng: **PostgreSQL 18** ✅ *(đã xem xét lại sau phản biện)*

> **Bối cảnh:** chủ dự án phản biện rằng *"Postgres khá nặng khi vận hành so với MySQL"*.
> Sau kiểm chứng: **phản biện này phần lớn đúng**, và bản đầu của [ADR-001](adr/001-postgres-everywhere.md)
> có **hai khẳng định sai** đã được đính chính.

**Những gì tôi viết sai:** `FOR UPDATE SKIP LOCKED` và `CHECK constraint` được liệt kê như
lợi thế riêng của Postgres. **Cả hai đều sai** — MySQL 8.0 có `SKIP LOCKED` (từ 8.0.1) và
8.0.16+ thực thi `CHECK`. Khoảng cách tính năng nhỏ hơn nhiều so với tôi trình bày.

**Những gì phản biện đúng:**

| | Đã kiểm chứng |
|---|---|
| RAM | Postgres tốn **hơn 30–40%** cho cùng tập dữ liệu |
| Kết nối | Postgres process-per-connection; MySQL thread-per-connection (nhẹ hơn) |
| Vacuum | *"Mối lo vận hành lớn nhất của Postgres, và là thứ hay vấp nhất khi chuyển từ MySQL"* |
| Edge | Có nguồn khuyến nghị MySQL cho *"VPS nhỏ hoặc triển khai edge"* |

**Nhưng:** cả bốn điểm yếu đều **tỷ lệ thuận với quy mô**, mà DB cửa hàng rất nhỏ — vài trăm
MB, một app server, pool ~10 kết nối, không replication. 30–40% của 256 MB là ~80 MB. Ở mức
này cả hai engine đều gần như không cần vận hành gì.

#### Ba phương án

| | **A. Postgres** *(đang đề xuất)* | **B. MySQL 8** | **C. SQLite** |
|---|---|---|---|
| Vận hành | Nhẹ ở quy mô này, nhưng phải chỉnh autovacuum cho `outbox` | Nhẹ nhất trong nhóm có daemon | **Bằng 0** — không daemon, sao lưu = copy file |
| RAM/cửa hàng | ~256 MB | ~180 MB | **~0** (trong tiến trình app) |
| Số engine phải vận hành | **1** (giống trung tâm) | **2** ⚠️ | **2** ⚠️ |
| Partial index cho `outbox` | ✅ | ❌ (index toàn bảng) | ✅ |
| Transactional DDL *(migration lỗi giữa chừng)* | ✅ rollback trọn vẹn | ❌ để lại schema nửa vời | ✅ |
| `uuidv7()` native | ✅ PG 18 | ❌ | ❌ (sinh ở app) |
| Nhiều instance app/cửa hàng | ✅ | ✅ | ❌ |
| Dùng chung code/migration với trung tâm | ✅ | ❌ | ❌ |

#### Lập luận quyết định: **số lượng engine, không phải lựa chọn engine**

Câu hỏi đúng không phải *"Postgres hay MySQL nặng hơn"* mà là:

> **Một Postgres** nặng hơn, hay **một MySQL + một Postgres** nặng hơn?

DB trung tâm **phải** là Postgres (phân vùng `point_ledger`, `uuidv7()`, và nhất là
transactional DDL cho migration của schema tài chính). Nên chọn MySQL ở cửa hàng = **vận
hành hai engine**: hai quy trình backup, hai bộ giám sát, hai đường nâng cấp, hai phương ngữ
SQL, hai bộ driver/testcontainers, hai bộ cạm bẫy.

**Và v1 đã chứng minh chi phí này là thật:** v1 chạy MySQL *và* Postgres ở cửa hàng, rồi
schema trôi khỏi code ở **cả hai** (lỗi B2, B5, B7 trong [09-v1-postmortem.md](09-v1-postmortem.md)).

#### Khuyến nghị

**Giữ Postgres (A)** — nhưng với lý do đã đổi: không phải *"Postgres có tính năng MySQL
không có"* (phần lớn sai), mà là *"ở quy mô cửa hàng, khác biệt vận hành giữa hai engine
gần như bằng 0, trong khi chi phí vận hành engine **thứ hai** thì không"*.

**Việc bắt buộc kèm theo:** cấu hình `autovacuum_vacuum_scale_factor` riêng cho bảng `outbox`
ngay từ migration đầu — đây là bảng duy nhất có mẫu sinh bloat.

**Nếu bạn vẫn muốn đổi:** phương án đáng cân nhắc hơn MySQL là **SQLite (C)** — nó trả lời
mối lo vận hành của bạn triệt để hơn hẳn (vận hành *bằng 0*, không chỉ *nhẹ hơn*). Đánh đổi
là mất khả năng chạy nhiều instance app trong một cửa hàng và mất việc dùng chung migration
với trung tâm. Với 1–4 quầy thu ngân và tải ~1 đơn/30 giây lúc cao điểm, giới hạn một-writer
của SQLite WAL **không phải vấn đề**.

**🔁 Đảo ngược:** Vừa — SQLAlchemy che phần lớn khác biệt, nhưng migration và SQL đặc thù
phải viết lại. Nên chốt **trước tuần 1**.

---

### B1. ClickHouse làm warehouse 🟡

**Lý do chọn:** là **server** ngay từ đầu (Metabase kết nối bền, nhiều người dùng đồng
thời), xử lý hàng tỷ dòng trên một node (T3 = 2 tỷ dòng/năm vẫn trong tầm), ~1 GB RAM trên
laptop, **đọc Parquet trên MinIO trực tiếp bằng `s3()`** → nạp lake→DW chỉ là một câu SQL,
và có ClickHouse Cloud làm đường lên production mà không đổi SQL.

**Đã loại — và đây là chỗ đáng bạn xem lại:**

| Phương án | Vì sao loại |
|---|---|
| **DuckDB** | **Nhúng trong tiến trình** — không phục vụ BI nhiều người dùng, không có server cho Metabase giữ kết nối. Sẽ phải di trú ở T2. *(Vẫn dùng DuckDB cho khám phá ad-hoc trên Parquet — công cụ tốt, chỉ không phải warehouse)* |
| **Postgres làm luôn DW** | Chạy được ở T1, sập ở T2+ (row-store quét 128 triệu dòng). Chọn = chấp nhận một cuộc di trú đã biết trước |
| **BigQuery/Snowflake** | Không chạy local được (vi phạm ràng buộc), tốn tiền |
| **Iceberg + Trino** | Trino ngốn RAM, thêm hẳn một tầng khái niệm. Thừa cho POC 1 tháng |

**⚠️ Phát hiện mới cần bạn biết:** **DuckLake v1.0** (production-ready từ 04/2026) đặt
metadata lakehouse **trong Postgres** — mà ta đã có sẵn. Tra metadata nhanh 10–100× so với
Iceberg, ACID đa bảng, schema evolution, time travel. Hồ sơ áp dụng của nó — *"lakehouse
local/nhúng, nhóm nhỏ dùng chung catalog Postgres"* — **khớp gần như hoàn hảo với dự án này**.
Tôi vẫn không chọn cho v1 vì: (a) vẫn vướng vấn đề DuckDB không có server cho BI, (b) mới
~5 tháng ở mức production, hệ sinh thái ngoài DuckDB còn hẹp. **Là ứng viên số 1 cho v2.**

**Ghi chú kỹ thuật quan trọng đã kiểm chứng:** `ReplacingMergeTree` chỉ cho tính đúng đắn
**eventual** — `SELECT` thường vẫn có thể trả dòng trùng, phải dùng `FINAL`. Nên **không
được dựa vào nó một mình cho FR-C06**. Cơ chế chính phải là `insert_deduplication_token`
(retry cùng token → ClickHouse bỏ qua hẳn lần insert) + thay trọn phân vùng khi backfill.
Nếu không biết điều này, test AT-07 sẽ cho kết quả sai lệch.

**🔁 Đảo ngược:** Vừa — logic nằm trong dbt nên đổi engine là đổi adapter + phương ngữ.
→ [ADR-005](adr/005-clickhouse-warehouse.md)

---

### B2. MinIO + Parquet làm lake 🟢

**Lý do:** S3 API (đổi sang S3/GCS/R2 là đổi endpoint, 1 giờ), chạy local, bất biến, nén
tốt. Bronze bất biến = **warehouse luôn dựng lại được từ đầu** — lưới an toàn quan trọng
nhất khi (chắc chắn) phát hiện logic biến đổi sai.

**Vì sao chưa dùng Iceberg:** Iceberg là đích đến đúng, nhưng thêm một tầng khái niệm +
bộ công cụ trong khi ngân sách 1 tháng. Parquet phân vùng Hive giải quyết 90% nhu cầu v1.
**Nâng cấp rẻ:** `pyiceberg` bọc được layout Parquet hiện có — chuyển đổi là sinh metadata,
không phải viết lại dữ liệu.

**🔁 Đảo ngược:** Rẻ.

---

### B3. dbt-core làm tầng biến đổi 🟢

**Lý do:** công cụ **đòn bẩy cao nhất trong cả stack** với solo dev. Miễn phí ngay khi cài:
incremental model, test dữ liệu (`not_null`/`unique`/`relationships`/`accepted_values`),
đồ thị lineage, docs tự sinh, môi trường dev/prod.

**FR-C06 (idempotent) và FR-C07 (data quality) — hai lỗi nặng nhất của v1 — gần như được
dbt cho không.**

**Đã kiểm chứng rủi ro pháp lý/sở hữu:** dbt Labs sáp nhập Fivetran hoàn tất 01/06/2026.
**dbt Core v2.0 kèm Fusion engine (viết lại bằng Rust, parse nhanh hơn nhiều) vẫn open
source Apache 2.0**, công ty cam kết tiếp tục duy trì dbt Core. → Rủi ro thấp, và Fusion
là lợi ích thêm.

**Đã loại — đáng bạn cân nhắc:** **SQLMesh** hiểu native "time-partitioned incremental,
idempotent incremental" và **xử lý dữ liệu đến trễ đúng theo mặc định** — trong khi
incremental của dbt về bản chất là *template truy vấn*, việc phát hiện thay đổi là trách
nhiệm của người dùng. Dữ liệu đến trễ **đúng là đặc tính của dự án này** (cửa hàng offline
đồng bộ muộn), nên SQLMesh khớp về mặt kỹ thuật. Tôi vẫn chọn dbt vì: hệ sinh thái lớn hơn
nhiều, nhiều tài liệu/ví dụ hơn (quan trọng khi đang học), và lời khuyên phổ biến của ngành
2026 là *"nhóm nhỏ hoặc mới bắt đầu thì dùng dbt; nền tảng lớn từng bị lỗi incremental thì
SQLMesh đáng đầu tư"*. **Nếu bạn muốn ưu tiên đúng đắn hơn độ phổ biến, SQLMesh là lựa chọn
hợp lý** — nói tôi biết.

**🔁 Đảo ngược:** Vừa (viết lại model sang cú pháp khác).

---

### B4. ~~Dagster~~ → **Airflow 3 + LocalExecutor** ✅ *ĐÃ CHỐT 2026-09-11*

> **Quyết định của chủ dự án: dùng Airflow** — đã có kinh nghiệm sử dụng.
>
> Đây là lý do hợp lệ và **không mâu thuẫn** với nguyên tắc "ưu tiên độ phù hợp thực tế,
> không chạy theo độ phổ biến": với ngân sách 1 người / 1 tháng, "công cụ tôi gỡ lỗi được
> lúc 11 giờ đêm" là một ràng buộc kỹ thuật thật. Và lập luận kỹ thuật chính để chọn Dagster
> (mô hình asset) đã mất giá trị khi Airflow 3.2 bổ sung asset partitioning (04/2026).
>
> **Ràng buộc kèm theo — bắt buộc:** dùng **LocalExecutor** (5 container thay vì 7, bỏ Celery
> worker + Redis, tiết kiệm ~800 MB) và **tách compose profile** để edge và data platform
> không chạy đồng thời. Thêm **`astronomer-cosmos`** để giữ lineage dbt.
>
> → Chi tiết và bảng "bài học từ v1 phải tránh" ở [ADR-007](adr/007-airflow-over-dagster.md).

<details>
<summary>Phân tích gốc (giữ lại để tham chiếu)</summary>

#### Dagster làm orchestrator 🔴 *sát nút nhất*

**Đây là quyết định tôi tự tin thấp nhất, và tôi đã phải sửa lập luận ban đầu sau khi kiểm
chứng.**

**Lý do còn đứng vững — tài nguyên:**

| | Container | RAM |
|---|---:|---:|
| **Airflow 3** (webserver, scheduler, triggerer, dag-processor, worker + Postgres + Redis) | **7** | Docs chính thức: **≥ 4 GB, khuyến nghị 8 GB** |
| **Dagster** | 1–2 | ~700 MB |

> ✅ **Đo thật (2026-09-24):** Airflow 3.3.2 LocalExecutor chạy **4 container** (api-server,
> scheduler, dag-processor, DB meta; bỏ triggerer), ~1,8 GB lúc nghỉ, scheduler đỉnh 2,1 GB. Cả
> stack 3 cửa hàng + trung tâm + data platform: ~3,4 GiB, đỉnh 3,9 GiB ([02](02-scale-capacity.md)).
> Nỗi lo "vượt trần" dưới đây không xảy ra với LocalExecutor.

Airflow 3 **nặng hơn** Airflow 2 (tách `triggerer` và `dag-processor` ra riêng). Tổng stack
v2 dự kiến ~5 GB trên máy 16 GB — **riêng Airflow ở mức khuyến nghị đã ăn 8 GB**. Cộng 3
cửa hàng mô phỏng + ClickHouse + MinIO thì vượt trần.

**Lý do tôi phải rút lại:** lập luận ban đầu của tôi ("chỉ Dagster có asset + partition hạng
nhất") **không còn đúng**. Airflow 3.0 (04/2025) đã có asset-aware scheduling; **Airflow 3.2
(07/04/2026) bổ sung asset partitioning** — kích hoạt theo *một phân vùng* của asset thượng
nguồn. Airflow 3.2/3.3 đã đóng phần lớn khoảng cách. Lợi thế còn lại của Dagster chỉ là *độ
mượt* của mô hình, không phải sự tồn tại của nó.

**Lập luận ngược (trình bày thẳng):** Airflow có **nguồn tuyển dụng lớn nhất** trong
orchestration. Bạn cũng **đã quen** Airflow (đã viết 2 DAG). Nếu dự án này có khả năng thành
portfolio xin việc, đó là lập luận mạnh.

**Đánh giá của ngành khớp đúng bối cảnh này:** *Dagster phù hợp cho dự án greenfield,
dbt-centric, một nhóm analytics; Airflow mạnh hơn cho nền tảng nhiều nhóm cần độ rộng hệ
sinh thái và governance.* Dự án này là trường hợp đầu.

**Có một cách dung hòa:** nếu bạn chấp nhận chạy data platform **tách riêng** khỏi edge
(bật/tắt bằng compose profile, không chạy đồng thời), ràng buộc RAM biến mất và **Airflow 3
trở lại ngang ngửa**. Lúc đó nên chọn theo giá trị CV.

**Giảm thiểu rủi ro (đã thiết kế sẵn):** giữ **toàn bộ logic nghiệp vụ trong dbt và hàm
Python thuần**; orchestrator chỉ là lớp vỏ điều phối. Đổi Dagster ↔ Airflow ước tính 1–2
ngày, không phải viết lại pipeline.

**🔁 Đảo ngược:** Rẻ — có chủ đích. → [ADR-006](adr/006-dagster-over-airflow.md)

</details>

---

### B5. Các lựa chọn còn lại 🟢 *(ít tranh cãi)*

| Thành phần | Chọn | Lý do ngắn |
|---|---|---|
| Ngôn ngữ | **Python 3.12+** | Một ngôn ngữ cho API + pipeline + dbt. Solo dev không đủ ngân sách cho hai hệ sinh thái |
| Quản lý gói | **uv** | Nhanh hơn pip 10–100×, lockfile chuẩn, thay luôn pyenv/virtualenv |
| API | **FastAPI** | Async, tự sinh OpenAPI, validate Pydantic. Thay Flask để thống nhất |
| Config/DTO | **Pydantic v2 + pydantic-settings** | Một mô hình cho DTO, cấu hình, biên dữ liệu. Chặn lỗi secret hardcode của v1 |
| ORM | **SQLAlchemy 2 (async) + Alembic** | Migration có version — chặn lỗi "schema trôi khỏi code" của v1 |
| Cache | **Redis 7** | Cache khách + phiên + rate limit. Thật lòng: ở T1–T2 Postgres đủ nhanh; Redis tốn 64MB, ~30 dòng code. **Bắt buộc: tắt Redis hệ thống vẫn chạy** (AT-08) |
| BI | **Metabase** | 1 container, người không biết SQL vẫn dùng được. Driver ClickHouse ổn |
| Auth | **JWT (PyJWT) + Argon2id** | Stateless, hợp edge. Token mang `store_id` |
| Log | **structlog** → JSON + `trace_id` | Truy vết xuyên cửa hàng → trung tâm |
| Test | **pytest + testcontainers** | DB thật, không mock — chặn lỗi schema/code lệch nhau |
| Code quality | **ruff + mypy** | Một công cụ thay black/isort/flake8 |
| Ranh giới module | **import-linter** | Cưỡng chế ADR-004 bằng CI |
| Đóng gói | **Docker multi-stage + Compose profiles** | `--profile edge/central/data/bi` để chạy từng phần trên laptop |
| CI | **GitHub Actions** | lint → type → test → build |

---

## Nhóm C — Đã chốt 2026-09-11

Chủ dự án yêu cầu chốt toàn bộ kịch bản trước, kiểm chứng để sau. Chi tiết lý do từng quyết
định ở [ADR-008](adr/008-remaining-decisions.md).

| # | Câu hỏi | Chốt | Lý do một dòng |
|---|---|---|---|
| C0 | DB tại cửa hàng | **PostgreSQL 18** | Số lượng engine mới là chi phí thật, không phải lựa chọn engine |
| C1 | UI POS | **HTMX + Alpine.js** | Tránh toolchain thứ hai; logic nghiệp vụ ở lại một chỗ |
| C2 | Orchestrator | **Airflow 3 + LocalExecutor** | Chủ dự án đã có kinh nghiệm |
| C3 | Transform | **dbt-core** | Vấn đề dữ liệu đến trễ giải bằng thiết kế phân vùng, không cần đổi công cụ |
| C4 | Redeem điểm | **Không ở v1** | Tích điểm an toàn offline, **tiêu điểm thì không** — thao tác duy nhất phá vỡ offline-first |
| C5 | Multi-tenant | **Không** | Database-per-tenant tốt hơn và không tốn gì bây giờ |

## §4. Tổng kết để chốt

| # | Quyết định | Chốt | 🔁 Đảo |
|---|---|---|:---:|
| A1 | **PostgreSQL 18** ở trung tâm | ✅ | Đắt |
| A2 | **Point ledger append-only** | ✅ | Rất đắt |
| A3 | **Outbox + HTTP** (Kafka → v2 qua Redpanda) | ✅ | Rẻ |
| A4 | **Modular monolith** ở edge + `import-linter` | ✅ | Rẻ* |
| B0 | **PostgreSQL 18** ở cửa hàng *(+ chỉnh autovacuum cho `outbox`)* | ✅ | Vừa |
| B1 | **ClickHouse** warehouse | ✅ | Vừa |
| B2 | **MinIO + Parquet** lake *(phân vùng theo ngày nạp)* | ✅ | Rẻ |
| B3 | **dbt-core** + `astronomer-cosmos` | ✅ | Vừa |
| B4 | **Airflow 3 + LocalExecutor** | ✅ *(chủ dự án)* | Rẻ |
| B5 | Python 3.12 · FastAPI · Redis · Metabase · pytest · ruff/mypy | ✅ | Rẻ |
| C1 | **HTMX + Alpine.js**, không build step | ✅ | Rẻ |
| C4 | Redeem điểm — **không làm v1** | ✅ | — |
| C5 | Multi-tenant — **không**, dùng database-per-tenant nếu cần | ✅ | Vừa |
| Q2 | Trích xuất bằng **Python thuần**, không `dlt` | ✅ | Rẻ |

\* rẻ *nếu* ranh giới module được cưỡng chế bằng CI.

### Ba ràng buộc thiết kế phát sinh từ các quyết định trên

Đây là những thứ dễ quên nhưng sẽ gây lỗi nặng nếu bỏ sót:

1. **`ALTER TABLE outbox SET (autovacuum_vacuum_scale_factor = 0.02, ...)`** ngay ở migration
   đầu — trả lời phản biện về autovacuum của Postgres ([ADR-008 C0](adr/008-remaining-decisions.md))
2. **Bronze phân vùng theo `recorded_at` (ngày nạp), KHÔNG theo `occurred_at`** — làm cho
   dữ liệu đến trễ không bao giờ phải viết lại lịch sử ([ADR-008 C3](adr/008-remaining-decisions.md))
3. **`insert_deduplication_token` là cơ chế idempotency chính** của ClickHouse, không phải
   `ReplacingMergeTree` (vốn chỉ eventual, cần `FINAL`) ([ADR-005](adr/005-clickhouse-warehouse.md))

\* rẻ *nếu* ranh giới module được cưỡng chế bằng CI.

**Cân đo so với stack v1:**

| | v1 | v2 |
|---|---:|---:|
| Hệ thống hạ tầng | 8 | 5 |
| RAM (3 cửa hàng) | ~10.2 GB | ~5 GB |
| Phương ngữ SQL | 4 | 2 |
| Thời gian dựng hạ tầng | ~2 tuần | ~4 ngày |

**Nếu bạn chốt tất cả như đề xuất**, bốn ngày tiết kiệm ở hạ tầng dồn vào tuần 2 — tuần khó
nhất và tạo giá trị thật nhất ([06-roadmap.md](06-roadmap.md)).

---

## §5. Nhật ký nghiên cứu (2026-09-11)

Những điểm đã kiểm chứng bằng nguồn ngoài, vì knowledge cutoff của tôi là 05/2026:

| Kiểm chứng | Kết quả | Ảnh hưởng |
|---|---|---|
| PostgreSQL 18 ổn định chưa? | Phát hành 25/09/2025, hiện **18.6** (13/08/2026). Có `uuidv7()` native + async I/O (tới 3× nhanh hơn đọc storage) | **Nâng đề xuất từ PG 16 → PG 18** |
| Airflow 3 có nhẹ hơn không? | **Không — nặng hơn.** 7 container, docs khuyến nghị 8 GB | Củng cố ADR-006 |
| Airflow 3 có asset/partition chưa? | **Có.** 3.0 có asset-aware; **3.2 (04/2026) có asset partitioning** | **Rút lại một phần lập luận ADR-006** |
| dbt Core còn open source sau sáp nhập Fivetran? | **Còn.** Sáp nhập xong 01/06/2026; dbt Core v2.0 + Fusion engine (Rust) vẫn Apache 2.0 | Rủi ro thấp, giữ dbt |
| Có nên dùng SQLMesh? | Xử lý dữ liệu đến trễ + idempotent incremental tốt hơn theo mặc định. Nhưng lời khuyên ngành: nhóm nhỏ/mới → dbt | Nêu thành lựa chọn C3 |
| DuckLake đã dùng được chưa? | **v1.0 production-ready 04/2026.** Metadata trong Postgres, nhanh 10–100× Iceberg. Hồ sơ áp dụng khớp dự án này | Ứng viên số 1 cho v2 |
| ClickHouse idempotency đúng cách? | **`insert_deduplication_token`** là cơ chế chính; `ReplacingMergeTree` chỉ eventual, cần `FINAL` | **Sửa thiết kế FR-C06 / AT-07** |
| Redpanda vs Kafka cho v2? | Redpanda ít tài nguyên hơn **3–6×**, một binary, không ZooKeeper, cùng API | Chốt Redpanda cho đường nâng cấp |
| Có sync engine offline-first dùng được? | **PowerSync** production-tested nhất (SOC2/HIPAA 01/2026); **ElectricSQL đã gia nhập Databricks 11/08/2026**. Nhưng cả hai giải bài toán *thiết bị ↔ cloud* (Postgres → SQLite trên máy khách), không phải *server cửa hàng ↔ server trung tâm* | **Không phù hợp v1.** Ghi nhận: nếu sau này muốn từng *quầy* chạy được khi server cửa hàng chết, PowerSync mới thành liên quan |

### Nguồn

- [PostgreSQL 18 Release Notes](https://www.postgresql.org/docs/release/18.0/) · [UUIDv7 Comes to PostgreSQL 18](https://www.thenile.dev/blog/uuidv7) · [PostgreSQL 18.6 released](https://www.postgresql.org/about/news/postgresql-186-1711-1615-1519-1424-and-19-beta-3-released-3365/)
- [Running Airflow in Docker — Airflow 3.3.1 Docs](https://airflow.apache.org/docs/apache-airflow/stable/howto/docker-compose/index.html) · [Orchestration in 2026: Airflow 3 vs Dagster vs Prefect](https://datalakehousehub.com/blog/orchestration-in-2026/) · [Airflow vs Dagster 2026 — Astronomer](https://www.astronomer.io/airflow/astro-vs-dagster/)
- [Fivetran + dbt Labs Complete Merger](https://www.fivetran.com/press/fivetran-dbt-labs-complete-merger-to-create-the-data-infrastructure-for-trusted-ai-agents) · [Fivetran–dbt Merger: Future of Open Source and dbt Core — Nexla](https://nexla.com/blog/open-source-vs-saas-fivetran-dbt-merger)
- [dbt-core vs SQLMesh in 2026](https://ai2sql.io/ai-blog/dbt-core-vs-sqlmesh-in-2026-which-sql-transformation-tool-should-you-pick) · [SQLMesh Comparisons](https://sqlmesh.readthedocs.io/en/stable/comparisons/)
- [DuckLake v1.0 Production-Readiness](https://ducklake.select/2026/04/13/ducklake-10/) · [Lakehouse Table Formats in 2026](https://amdatalakehouse.substack.com/p/lakehouse-table-formats-in-2026-iceberg) · [Duck Lake vs Iceberg: An Operator's Verdict](https://www.definite.app/blog/duck-lake-vs-iceberg)
- [ClickHouse Deduplication Strategies](https://clickhouse.com/docs/guides/developer/deduplication) · [Insert Deduplication / Insert Idempotency — Altinity KB](https://kb.altinity.com/altinity-kb-schema-design/insert_deduplication/)
- [Redpanda vs Kafka overview](https://www.redpanda.com/compare/redpanda-vs-kafka) · [Self-hosted Kafka: Redpanda vs vanilla](https://www.bigiron.cc/guides/self-hosted-kafka-redpanda-vs-vanilla-confluent-platform)
- [ElectricSQL vs PowerSync vs Zero (2026)](https://trybuildpilot.com/648-electric-sql-vs-powersync-vs-zero-2026) · [ClickHouse vs DuckDB 2026](https://tasrieit.com/blog/clickhouse-vs-duckdb-2026)

---
*Changelog: 2026-09-25 — thêm số đo RAM thật của Airflow 3 cạnh phần ước lượng.*

*Changelog: 2026-09-11 — tạo mới sau vòng nghiên cứu kiểm chứng.*
