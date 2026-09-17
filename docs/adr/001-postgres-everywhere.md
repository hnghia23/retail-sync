# ADR-001 — PostgreSQL cho mọi OLTP

**Trạng thái:** Được chấp nhận · 2026-09-11
**Thay thế:** thiết kế v1 (MySQL ở cửa hàng + Cassandra ở trung tâm)

## Bối cảnh

Thiết kế v1 dùng ba engine OLTP: MySQL cho POS cửa hàng, PostgreSQL cho Loyalty cửa hàng,
Cassandra cho Loyalty trung tâm. Lý do ngầm là "Cassandra để scale" và "MySQL cho POS".

Số liệu thực tế từ [02-scale-capacity.md](../02-scale-capacity.md):

| | T1 (20 CH) | T2 (200 CH) | T3 (2000 CH) |
|---|---:|---:|---:|
| Ghi đỉnh | < 1 /s | ~4 /s | ~67 /s (~200 /s khi bùng nổ) |
| Dòng/năm | 2.2 Tr | 36.5 Tr | 584 Tr |
| Dung lượng/năm | 1.1 GB | 17.5 GB | 280 GB |

Một PostgreSQL node xử lý 5 000–10 000 writes/giây.

## Quyết định

Dùng **PostgreSQL 18 cho mọi cơ sở dữ liệu vận hành** — cả ở cửa hàng và trung tâm. Bỏ
MySQL. Bỏ Cassandra.

## Lý do

### Vì sao bỏ Cassandra

1. **Dư địa sai chỗ 25–100 lần.** Cassandra sinh ra cho hàng chục nghìn writes/giây và dữ
   liệu vượt một máy. Hệ thống này chạm mức đó ở khoảng **200 000 cửa hàng**.

2. **Cassandra sai về bản chất cho sổ cái tài chính.** Điểm tích lũy là tiền. Cassandra
   không có transaction đa dòng, không có khóa ngoại, và `UPDATE points = points + X`
   là **race condition** — hai cửa hàng ghi cùng lúc thì last-write-wins, mất điểm của
   khách. Muốn an toàn phải dùng counter column (không đọc chính xác được) hoặc LWT
   (chậm và phức tạp).

3. **Chi phí vận hành.** Tối thiểu 3 node để có ý nghĩa. Phải hiểu compaction, tombstone,
   repair, tuning. Một người không kham nổi song song với việc phát triển.

4. **Không join được.** Mọi truy vấn phân tích đơn giản đều phải làm bằng tay ở tầng ứng dụng.

### Vì sao bỏ MySQL

> ⚠️ **ĐÍNH CHÍNH 2026-09-11.** Bản đầu của ADR này liệt kê `FOR UPDATE SKIP LOCKED` và
> `CHECK constraint` như lợi thế riêng của Postgres. **Cả hai đều sai** — MySQL 8.0 có
> `SKIP LOCKED` (từ 8.0.1) và MySQL 8.0.16+ thực thi `CHECK constraint`. Bảng dưới là bản
> đã kiểm chứng lại. Khoảng cách tính năng **nhỏ hơn nhiều** so với những gì tôi viết ban đầu.

| Tính năng | Postgres 18 | MySQL 8.0+ | Có thực sự khác biệt cho dự án này? |
|---|---|---|---|
| `FOR UPDATE SKIP LOCKED` | ✅ | ✅ (từ 8.0.1) | ❌ **Không.** Cả hai đều làm được outbox worker |
| `CHECK` constraint | ✅ | ✅ (từ 8.0.16) | ❌ **Không** |
| Chèn bỏ qua trùng | `ON CONFLICT DO NOTHING` | `INSERT IGNORE` / `ON DUPLICATE KEY` | ❌ **Không.** Tương đương thực dụng |
| Table partitioning | ✅ declarative | ✅ | ❌ Không đáng kể |
| JSON | JSONB + GIN | JSON | 🟡 Nhẹ — Postgres tốt hơn nhưng MySQL đủ dùng |
| **Partial index** | ✅ | ❌ **Không có** (bug #76631 vẫn mở) | ✅ **Có.** `INDEX ON outbox(id) WHERE sent_at IS NULL` giữ index nhỏ mãi mãi. MySQL phải index toàn bảng hoặc dùng generated column |
| **Transactional DDL** | ✅ | ❌ DDL gây implicit commit (MySQL 9.0 có *atomic* DDL, **không phải** transactional) | ✅ **Có.** Migration Alembic thất bại giữa chừng trên Postgres sẽ rollback trọn vẹn; trên MySQL để lại schema nửa vời phải sửa tay |
| **`uuidv7()` native** | ✅ (PG 18) | ❌ Chỉ có `UUID_TO_BIN(uuid,1)` cho v1 hoán byte | ✅ **Có** |

**Kết luận trung thực:** chỉ còn **ba** khác biệt thật sự quan trọng (partial index,
transactional DDL, `uuidv7()`), và không cái nào tự nó đủ để loại MySQL. Lý do quyết định
nằm ở chỗ khác — xem §"Lập luận thật sự" bên dưới.

### Lập luận thật sự: số lượng engine, không phải lựa chọn engine

Câu hỏi đúng **không phải** "Postgres hay MySQL nặng hơn", mà là:

> **Một Postgres** nặng hơn, hay **một MySQL + một Postgres** nặng hơn?

DB trung tâm **phải** là Postgres (phân vùng `point_ledger`, `uuidv7()`, và nhất là
transactional DDL cho migration của một schema tài chính). Nên chọn MySQL ở cửa hàng nghĩa
là **vận hành hai engine**:

| Chi phí khi có 2 engine | Cụ thể |
|---|---|
| Hai quy trình sao lưu/phục hồi | `pg_dump` + `mysqldump`, hai chiến lược PITR |
| Hai bộ giám sát | Chỉ số, cảnh báo, ngưỡng khác nhau |
| Hai đường nâng cấp phiên bản | Lịch phát hành, EOL khác nhau |
| Hai phương ngữ SQL | Kiểu dữ liệu, hàm, cú pháp upsert khác nhau |
| Hai bộ driver + testcontainers | Trong CI và test tích hợp |
| Hai bộ "cạm bẫy" phải thuộc | Vacuum/bloat bên này, implicit commit/charset bên kia |

Và **v1 đã chứng minh chi phí này là thật**: v1 chạy MySQL *và* Postgres ở cửa hàng, rồi
schema trôi khỏi code ở **cả hai** (lỗi B2 `unit_price` bên MySQL, lỗi B5/B7 schema và tên
cột bên Postgres — xem [09-v1-postmortem.md](../09-v1-postmortem.md)).

Với ngân sách 1 người / 1 tháng, đây là lập luận nặng nhất.

Và quan trọng hơn cả với solo dev: **dùng một engine = một phương ngữ SQL, một công cụ
migration, một chiến lược backup, một mô hình tư duy.** Mỗi engine thêm vào tốn hàng ngày
học và gỡ lỗi, cho toàn bộ vòng đời dự án.

### Vì sao cụ thể là PostgreSQL 18 *(cập nhật 2026-09-11 sau khi kiểm chứng)*

PG 18 (phát hành 25/09/2025, hiện ở minor 18.6 — đã chín) mang hai thứ **trực tiếp phục vụ
thiết kế này**:

| Tính năng PG 18 | Giá trị với dự án |
|---|---|
| **`uuidv7()` native** (RFC 9562) | Đúng cái ta cần cho `sale_id` và `event_id`. **Không cần extension.** Benchmark công bố trên 50 triệu dòng: bulk insert ~1.8 phút so với ~20 phút của UUIDv4, index nhỏ hơn ~25%, range scan nhanh ~3× |
| **Async I/O subsystem** | Tới 3× nhanh hơn ở tác vụ đọc storage trong một số workload — có lợi cho cả OLTP cửa hàng lẫn truy vấn trích xuất |
| `uuid_extract_timestamp()` hỗ trợ v7 | Lấy thời điểm trực tiếp từ ID, tiện cho gỡ lỗi và phân vùng |

Điều này giải quyết luôn một vấn đề thiết kế: v1 dùng `INT AUTO_INCREMENT` gây đụng ID giữa
các cửa hàng, nhưng UUIDv4 thì phá index. UUIDv7 cho cả hai: **duy nhất toàn cục và vẫn
sắp xếp theo thời gian**. Trước PG 18 phải dùng extension hoặc sinh ở tầng ứng dụng.

### Thừa nhận: Postgres *có* nặng hơn MySQL về vận hành

Đây là phản biện chính đáng, và phần lớn là đúng. Đã kiểm chứng:

| Điểm yếu của Postgres | Mức độ đúng | Có ảnh hưởng ở quy mô cửa hàng không? |
|---|---|---|
| **Autovacuum / bloat** — *"mối lo vận hành lớn nhất của Postgres, và là thứ hay vấp nhất khi chuyển từ MySQL"* | ✅ Đúng. MySQL/InnoDB dùng undo log + purge thread, tự động hơn | 🟡 **Nhỏ, nhưng có.** Bảng `outbox` đúng là mẫu sinh bloat (insert → update → delete). Ở 800 đơn/ngày/cửa hàng thì không đáng kể, nhưng **phải đặt `autovacuum_vacuum_scale_factor` riêng cho bảng `outbox`** |
| **Tốn RAM hơn 30–40%** cho cùng tập dữ liệu | ✅ Đúng | ❌ **Không.** DB cửa hàng vài trăm MB; chênh lệch trên ngân sách 256 MB là ~80 MB. Không đáng kể trên mini PC |
| **Process-per-connection** (MySQL dùng thread-per-connection, nhẹ hơn) | ✅ Đúng | ❌ **Không ở cửa hàng.** Một app server với pool ~10 kết nối. Vấn đề này chỉ xuất hiện ở hàng nghìn kết nối — tức là ở **trung tâm**, và đã có PgBouncer trong scale seam |
| Nâng cấp major version phiền hơn | ✅ Đúng (`pg_upgrade`) | 🟡 Thật, nhưng 1–2 năm mới xảy ra một lần |

**Tổng kết:** phản biện đúng về nguyên lý, nhưng **các điểm yếu của Postgres đều tỷ lệ thuận
với quy mô, mà DB cửa hàng thì rất nhỏ.** Ở mức vài trăm MB, một app server, không replication,
cả hai engine đều gần như không cần vận hành gì. Khác biệt chỉ lộ ra ở deployment lớn hoặc
nhiều kết nối — không phải hồ sơ của cửa hàng.

Việc bắt buộc kèm theo: **cấu hình autovacuum riêng cho bảng `outbox`** ngay từ migration đầu.

## Phương án đã xem xét và loại

| Phương án | Vì sao loại |
|---|---|
| Giữ Cassandra ở trung tâm | Không có con số nào biện minh. Chi phí thật, lợi ích bằng không ở mọi bậc quy mô mục tiêu |
| MongoDB thay cả hai | Tương tự — không giải quyết vấn đề nào ta thực sự có, và yếu hơn cho dữ liệu quan hệ như đơn hàng |
| **SQLite ở cửa hàng** *(xem lại 2026-09-11)* | **Đây mới là câu trả lời nhẹ nhất cho mối lo vận hành** — không daemon, không vacuum, không tuning, sao lưu = copy một file. Tải ghi của ta (1 đơn / ~30 giây lúc cao điểm) thừa sức cho giới hạn một-writer của WAL mode, vì app server vốn đã tuần tự hóa ghi. Nhưng: **dialect khác hẳn trung tâm** (mất luôn lợi ích "một phương ngữ" vốn là lập luận chính), khó quan sát/can thiệp từ xa, và chặn đường chạy nhiều instance app trong một cửa hàng. Đánh giá ngành: *"workload ghi nặng mang tính giao dịch (thanh toán, tồn kho) vẫn là lãnh địa PostgreSQL"* — nhưng cũng *"lợi thế của SQLite rõ nhất ở triển khai edge/một server"*. **Là ứng viên hợp lệ, xem [07 §B0](../07-stack-decision.md)** |
| CockroachDB ngay từ đầu | Tương thích Postgres và scale sẵn, nhưng nặng hơn nhiều trên laptop và thừa ở T0–T2. Là *đích đến*, không phải *điểm xuất phát* |
| Dùng một Postgres duy nhất, bỏ DB cửa hàng | Vi phạm ràng buộc cứng NFR-01 (bán hàng khi offline) |

## Hệ quả

### Tích cực
- Tiết kiệm ~3 GB RAM, ~1 tuần công
- Một phương ngữ SQL, một bộ công cụ
- Transaction thật cho ledger điểm — đúng đắn ngay từ nền
- Join được, kiểm tra ràng buộc được → chất lượng dữ liệu cao hơn tại nguồn
- Schema cửa hàng và trung tâm tương tự nhau → dùng chung được nhiều code

### Tiêu cực / rủi ro
- **Mất cơ hội "được ghi Cassandra vào CV".** Đây là chi phí thật nếu mục tiêu là học
  NoSQL phân tán — nhưng mục tiêu dự án là POC cho doanh nghiệp thật, nên tính đúng đắn
  thắng.
- Ở T3+, một Postgres trung tâm sẽ cần chăm sóc (phân vùng, replica, pooling). Nhưng đó
  là công việc **tăng dần**, không phải viết lại.

## Scale seams

Thứ tự nâng cấp khi chạm trần, theo [02 §4](../02-scale-capacity.md):

```
Postgres 1 node
   └─► + PgBouncer            (> 50 cửa hàng, ~1 ngày)
        └─► + Read replica     (khi đọc lấn át ghi, ~2 ngày)
             └─► + Partition   (> 200 Tr dòng, ~2 ngày — đã thiết kế sẵn)
                  └─► Citus hoặc CockroachDB  (> 1 Tỷ dòng, ~2 tuần)
```

Ba bước đầu **không đổi một dòng code ứng dụng**. Bước cuối tương thích phương ngữ Postgres
nên thay đổi ở mức tối thiểu.

## Điều kiện xem lại quyết định này

Viết ADR mới nếu **bất kỳ** điều nào xảy ra:
- Tải ghi thật vượt 2 000 writes/giây kéo dài
- Dữ liệu vận hành vượt 5 TB
- Cần ghi đa vùng địa lý với độ trễ thấp
- Đo được Postgres là nút cổ chai (bằng số liệu, không phải cảm giác)
