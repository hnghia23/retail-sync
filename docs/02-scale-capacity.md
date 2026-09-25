# 02 — Quy mô & Năng lực

> **Kết luận một dòng:** Ngay ở 2000 cửa hàng, tải ghi đỉnh chỉ khoảng **67 writes/giây**.
> Một PostgreSQL đơn lẻ dư 25–100 lần. **Bài toán này không phải bài toán throughput.**

Doc này tồn tại để chặn việc chọn công nghệ theo cảm tính. Mọi lần muốn thêm một hệ thống
phân tán, quay lại đây và chỉ ra con số nào biện minh cho nó.

---

## 1. Các bậc quy mô

| Bậc | Cửa hàng | Đơn/CH/ngày | Đơn/ngày | Ghi đỉnh (w/s) | Đơn/năm | Dòng item/năm | Thô GB/năm |
|---|---:|---:|---:|---:|---:|---:|---:|
| **T0** POC | 3 (mô phỏng) | 100 | 300 | < 1 | 110 K | 384 K | 0.06 |
| **T1** Pilot | 20 | 300 | 6 K | < 1 | 2.2 Tr | 7.7 Tr | 1.1 |
| **T2** Growth | 200 | 500 | 100 K | ~4 | 36.5 Tr | 128 Tr | 17.5 |
| **T3** Scale | 2 000 | 800 | 1.6 Tr | ~67 | 584 Tr | 2.0 Tỷ | 280 |

Giả định: 3.5 dòng sản phẩm/đơn; ~200 byte/đơn + 80 byte/dòng item ở dạng thô.

> ⚠️ **Đã hiệu chỉnh tải đỉnh (2026-09-11).** Bảng trên dùng giả định cũ 15% lượng ngày dồn
> vào giờ cao điểm. [B01 §2](business/01-operating-model.md) cho thấy thực tế có **hai đỉnh**:
> trưa ~25% và tối ~35% doanh thu ngày. Giờ cao điểm thật ≈ **35%**, không phải 15%.
>
> → **Ghi đỉnh thật ở T3 ≈ 130 writes/giây**, không phải 67. Cộng hệ số bùng nổ ×2 cho lễ Tết
> → **~260 w/s**.
>
> **Không đổi kết luận nào:** Postgres vẫn dư **20–40×**. Nhưng dùng con số 260 w/s khi thiết
> kế test tải, không dùng 67.

## 2. So với năng lực thực tế của công nghệ

| Thành phần | Năng lực 1 node | Cần ở T3 | Dư địa |
|---|---:|---:|---:|
| PostgreSQL (ghi OLTP) | 5 000–10 000 w/s | ~260 w/s *(đã hiệu chỉnh)* | **20–40×** |
| PostgreSQL (dung lượng) | 10+ TB khả thi | 280 GB/năm | **35×** (10 năm) |
| PostgreSQL (số dòng) | Hàng tỷ dòng có phân vùng | 584 Tr/năm | Ổn tới ~5 năm |
| ClickHouse (1 node) | Hàng tỷ dòng, quét < 1s | 2 Tỷ dòng item/năm | **Thoải mái** |
| Redis | 100 000+ ops/s | < 100 ops/s | **1 000×** |
| MinIO / S3 | Không giới hạn thực tế | 280 GB/năm | — |

### Tại sao điều này quan trọng

Cassandra tồn tại để giải bài toán **hàng chục nghìn writes/giây** và **dữ liệu vượt quá
một máy**. Hệ thống này chạm mức đó ở khoảng **200 000 cửa hàng** — gấp 100 lần mục tiêu
lớn nhất. Dùng Cassandra ở đây là trả toàn bộ chi phí (không transaction, không join,
eventual consistency, tối thiểu 3 node, gánh nặng vận hành) để đổi lấy **không gì cả**.

Xem [ADR-001](adr/001-postgres-everywhere.md).

## 3. Vậy cái gì mới thực sự khó?

Nếu không phải throughput, thì là bốn thứ sau. **Kiến trúc phải được thiết kế quanh những
cái này**, không phải quanh tốc độ ghi.

### 3.1. Phân mảnh mạng (khó nhất)

2000 cửa hàng × đường truyền không đảm bảo = **luôn luôn có cửa hàng đang offline**. Với
99.5% uptime đường truyền mỗi cửa hàng, kỳ vọng ~10 cửa hàng offline tại mọi thời điểm.

Hệ quả bắt buộc:
- Cửa hàng phải tự chủ hoàn toàn để bán hàng
- Mọi giao tiếp tới trung tâm phải bất đồng bộ, có hàng đợi bền vững
- Không bao giờ có giao dịch phân tán, không bao giờ khóa xuyên mạng

### 3.2. Ghi đồng thời vào cùng số dư điểm

Khách mua ở cửa hàng A và B gần như cùng lúc. Nếu mô hình là `UPDATE balance = balance + X`
thì hai lần ghi đua nhau, một cái đè lên cái kia → **mất điểm của khách**.

Đây là lý do chọn ledger append-only. Xem [ADR-002](adr/002-point-ledger.md).

### 3.3. Số lượng kết nối, không phải số lượng truy vấn

Ở T3: 2000 cửa hàng × mỗi cửa hàng vài kết nối = **hàng nghìn kết nối tới trung tâm**.
PostgreSQL mặc định `max_connections = 100`, mỗi kết nối tốn vài MB RAM.

Đây mới là giới hạn thật sẽ gặp trước tiên — và nó **không** được giải bằng cách đổi
database. Giải bằng: PgBouncer (pooling), API stateless phía trước DB, giao tiếp theo lô
thay vì từng dòng.

*Ghi nhớ: giới hạn đầu tiên bạn gặp sẽ là kết nối, không phải throughput.*

### 3.4. Dồn về (fan-in) và dội ngược (thundering herd)

Khi mạng phục hồi sau sự cố diện rộng, hàng trăm cửa hàng đồng loạt đẩy dữ liệu tồn đọng.

Giải pháp: jitter ngẫu nhiên khi retry, giới hạn tốc độ, xử lý theo lô, backpressure.

## 4. Điểm chuyển bậc — khi nào phải đổi gì

Bảng này là hợp đồng của NFR-08. Mỗi dòng là một **scale seam** cụ thể.

| Ngưỡng kích hoạt | Triệu chứng quan sát được | Hành động | Công sức |
|---|---|---|---|
| > ~50 cửa hàng | Kết nối tới Postgres trung tâm cạn | Thêm **PgBouncer** | 1 ngày |
| > ~100 cửa hàng | Sync worker HTTP không theo kịp lúc phục hồi | Đổi outbox → **CDC + Redpanda/Kafka**, code nghiệp vụ không đổi | 1 tuần |
| > ~200 Tr dòng ledger | Truy vấn ledger chậm dần | **Phân vùng theo tháng** (đã thiết kế sẵn) | 2 ngày |
| Đọc lấn át ghi | CPU Postgres cao do truy vấn đọc | Thêm **read replica**, tách đọc | 2 ngày |
| > ~1 Tỷ dòng ledger | Một node hết dư địa | **Citus / CockroachDB** (đều tương thích Postgres) | 2 tuần |
| ClickHouse 1 node hết dư | Dashboard chậm | Thêm shard, hoặc ClickHouse Cloud | 1 tuần |
| Cần T+0 thay vì T+1 | Nghiệp vụ đòi realtime | Dùng lại luồng CDC ở trên, nạp thẳng ClickHouse | 1 tuần |
| MinIO hết dung lượng | — | Đổi endpoint sang S3/GCS (cùng API) | 1 giờ |

**Không có dòng nào trong bảng này đòi viết lại từ đầu.** Đó chính là tiêu chí của một
kiến trúc "scale được".

## 5. Ngân sách tài nguyên cho POC

Toàn bộ phải chạy trên một laptop. Ước lượng RAM khi chạy 3 cửa hàng mô phỏng:

| Thành phần | RAM | Bắt buộc? |
|---|---:|---|
| PostgreSQL edge × 3 cửa hàng | 3 × 256 MB = 768 MB | Có |
| Redis × 3 | 3 × 64 MB = 192 MB | Có |
| API edge × 3 (FastAPI) | 3 × 150 MB = 450 MB | Có |
| PostgreSQL trung tâm | 512 MB | Có |
| API trung tâm | 200 MB | Có |
| MinIO | 300 MB | Có |
| ClickHouse | 1 GB | Có |
| **Airflow 3 — LocalExecutor** (web, scheduler, triggerer, dag-processor, Postgres-meta) | **~2 GB** | Có |
| Metabase | 900 MB | Nếu kịp |
| **Tổng nếu bật tất cả** | **~7 GB** | |

✅ **Đo thật (2026-09-24, `docker stats`, spike S1)** — ước lượng trên dư khoảng gấp đôi:

| Thành phần (đo) | RAM |
|---|---:|
| Mỗi stack cửa hàng (PG + Redis + API + sync worker) | ~230 MB |
| Postgres + API trung tâm | ~150 MB |
| MinIO / ClickHouse | ~100 MB / ~520 MB |
| Airflow 3.3.2: api-server / dag-processor / scheduler / DB meta | ~310 / ~250 / 1 160 (đỉnh 2 090) / ~80 MB |
| **3 cửa hàng + trung tâm + data platform (20 container)** | **~3,4 GiB**, đỉnh **~3,9 GiB** giữa lượt DAG |

Chưa có Metabase và `observability` (~0,5 GiB, đo 2026-09-18). Scheduler chiếm phần lớn khi các
task dbt chạy song song (LocalExecutor chạy task như tiến trình con của nó).

Vẫn vừa máy 16GB, nhưng sát hơn trước — đây là cái giá của [ADR-007](adr/007-airflow-over-dagster.md)
(Airflow thay Dagster: +~1.3 GB). **Bắt buộc dùng LocalExecutor**, không dùng CeleryExecutor
(bỏ được Celery worker + Redis = 2 container, ~800 MB).

Ngân sách theo từng kịch bản làm việc:

| Kịch bản | Profile | RAM |
|---|---|---:|
| Phát triển POS/Loyalty | `edge` + `central` | ~2.9 GB |
| Phát triển pipeline | `central` + `data` | ~4.2 GB |
| **Test tải / săn bottleneck** | `edge` + `central` + `observability` | **~4.4 GB** |
| Nghiệm thu đầy đủ *(2 cửa hàng)* | tất cả | **~8.5 GB** |

`grafana/otel-lgtm` thêm **~1–1.5 GB** nhưng chỉ khi bật profile `observability`
([ADR-009](adr/009-observability-stack.md)) — không ảnh hưởng lúc phát triển bình thường.

*(Để so sánh: stack dự kiến ban đầu — MySQL + Postgres + Redis + Kafka + Zookeeper +
Airflow + Cassandra + ClickHouse + MinIO — tốn khoảng **10+ GB** và khởi động rất chậm.
Phần tiết kiệm được đến từ việc bỏ Cassandra và Kafka, không phải từ orchestrator.)*

Docker Compose dùng **profile** để bật/tắt từng phần: `--profile edge`, `--profile central`,
`--profile data`, `--profile bi`.

## 6. Cách kiểm chứng các con số này

Đừng tin bảng trên. Ở **giai đoạn B** ([06](06-roadmap.md), [ADR-010](adr/010-data-flow-first.md)), chạy thật:

1. **Sinh dữ liệu**: bộ giả lập ([18](18-simulator.md)), profile `t0`…`t3` lấy đúng các bậc
   ở §1. Phân bố thực tế: hai đỉnh trưa/tối, cuối tuần nhiều hơn, Zipf cho sản phẩm.
2. **Đo tải**: cũng bộ giả lập đó, phát tải vòng hở vào Edge API và Central API, ghi p95/p99.
   Không dùng `locust`/`k6`, vì chúng sẽ thành bộ sinh dữ liệu thứ hai ([18 §9](18-simulator.md)).
3. **Đo dung lượng**: nạp 1 năm dữ liệu T2, đo kích thước thật của Postgres và ClickHouse.
4. **Thử hỗn loạn**: ngắt mạng bằng `docker network disconnect`, đo thời gian hội tụ sau
   khi nối lại.

Ghi kết quả thật ngược lại vào doc này, thay cho ước lượng.

---
*Changelog: 2026-09-25 — thêm RAM đo thật cạnh bảng ước lượng.*

*Changelog: 2026-09-23 — §6 trỏ về bộ giả lập và giai đoạn B (ADR-010).*

*Changelog: 2026-09-11 — tạo mới, số liệu là ước lượng chưa đo thật.*
