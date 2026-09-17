# 10 — Observability & Săn bottleneck

> Doc này trả lời: **khi test tải cho kết quả xấu, làm sao biết thời gian đi đâu mất?**
>
> Quyết định chọn stack ở [ADR-009](adr/009-observability-stack.md). Doc này là phần triển
> khai: đo cái gì, ở đâu, và cách đọc kết quả.

---

## 1. Stack

```
Ứng dụng (FastAPI, sync worker, Airflow)
   │  OpenTelemetry SDK — auto-instrumentation
   │  (FastAPI · SQLAlchemy · psycopg · redis · httpx)
   ▼  OTLP (gRPC :4317 / HTTP :4318)
┌─────────────────────────────────────────────┐
│  grafana/otel-lgtm   — MỘT container        │
│  OTel Collector → Prometheus (metrics)      │
│                  → Tempo      (traces)      │
│                  → Loki       (logs)        │
│                  → Grafana UI  :3000        │
└─────────────────────────────────────────────┘

Bổ sung ở tầng DB (không qua OTel):
  Postgres   → pg_stat_statements, EXPLAIN (ANALYZE, BUFFERS)
  ClickHouse → system.query_log
```

Chạy bằng `docker compose --profile observability up` — chỉ bật khi cần, không tốn RAM lúc
phát triển bình thường.

---

## 2. Bottleneck cần săn — theo từng đường đi

Không đo mò. Mỗi đường đi có **ngân sách thời gian** từ [NFR-02](01-requirements.md), và
tracing dùng để biết ngân sách đó tiêu ở đâu.

### 2.1. Chốt một đơn hàng — ngân sách p95 < 500 ms 🔴 *quan trọng nhất*

```
POST /sales                                        [ngân sách 500ms]
├─ xác thực JWT                                    ~5ms
├─ tải & kiểm tra sản phẩm trong giỏ               ~10ms   ⚠️ nghi vấn N+1
├─ xác định khách + hạng                           ~20ms
│  ├─ Redis GET                                    ~2ms    → đo cache hit rate
│  ├─ Postgres SELECT (khi miss)                   ~15ms
│  └─ HTTP trung tâm (khi miss cả hai)             ≤500ms  ⚠️ PHẢI có timeout
├─ tính tiền + điểm (hàm thuần)                    ~1ms
└─ TRANSACTION                                     ~50ms
   ├─ INSERT sale
   ├─ INSERT sale_line × N                         ⚠️ phải batch, không loop
   ├─ INSERT sale_payment × M
   ├─ INSERT point_ledger
   ├─ INSERT outbox
   └─ COMMIT (fsync)                               ⚠️ xem dưới
```

**Ba nghi vấn cần tracing xác nhận:**

1. **`synchronous_commit = on` tốn bao nhiêu?** Ta cố tình bật nó vì cửa hàng không có UPS
   ([08 §2.1](08-reliability-and-scale.md)). Mỗi commit phải chờ fsync — trên ổ đĩa rẻ có thể
   **5–20 ms**. Đây là đánh đổi **có chủ đích** (độ bền đổi lấy tốc độ), nhưng phải **đo** để
   biết mình đang trả giá bao nhiêu.
2. **N+1 khi tải sản phẩm** — lỗi kinh điển của ORM. Nếu span DB trong một request nhiều hơn
   ~5 cái, gần như chắc chắn có N+1.
3. **Lời gọi trung tâm có thực sự không chặn?** Trace phải chứng minh: khi trung tâm chết,
   span `POST /sales` vẫn kết thúc < 500 ms.

### 2.2. Sync worker

```
vòng lặp sync
├─ SELECT outbox ... FOR UPDATE SKIP LOCKED   ⚠️ partial index có được dùng không?
├─ POST /events (lô 100–500)
│  └─ [trung tâm] dedupe + INSERT             ⚠️ chi phí ON CONFLICT ở quy mô lớn
└─ UPDATE outbox SET sent_at
```

Câu hỏi cần trả lời: **kích thước lô tối ưu là bao nhiêu?** Lô lớn → ít round-trip nhưng
transaction dài. Tracing cho thấy điểm gãy.

### 2.3. Pipeline dữ liệu

```
Airflow DAG
├─ extract Postgres → Parquet      ⚠️ watermark có lọc đúng không, hay đang quét toàn bảng?
├─ upload MinIO
├─ ClickHouse INSERT FROM s3()
└─ dbt run (mỗi model một task nhờ astronomer-cosmos)   ⚠️ model nào chậm nhất?
```

`astronomer-cosmos` cho mỗi dbt model một task riêng → thấy ngay model nào là nút thắt, thay
vì một khối `dbt build` mờ đục.

---

## 3. Instrumentation — làm gì cụ thể

### 3.1. Auto-instrumentation lo phần lớn

```bash
uv add opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install      # tự phát hiện & cài instrumentation phù hợp
```

Tự động có span cho: request HTTP vào (FastAPI), truy vấn SQLAlchemy/psycopg, lệnh Redis,
request httpx đi ra. **Không cần sửa code.**

```yaml
# biến môi trường — không hardcode trong code
OTEL_SERVICE_NAME: edge-api
OTEL_RESOURCE_ATTRIBUTES: "store.id=store-001,deployment.environment=test"
OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-lgtm:4318
OTEL_TRACES_SAMPLER: parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG: "1.0"     # 100% khi test; giảm ở production
```

### 3.2. Span thủ công — chỉ cho ranh giới nghiệp vụ

Auto-instrumentation thấy kỹ thuật (HTTP, SQL) nhưng không thấy **ý nghĩa nghiệp vụ**. Thêm
span thủ công ở đúng những chỗ có ngân sách thời gian:

```python
with tracer.start_as_current_span("sale.resolve_customer") as span:
    span.set_attribute("cache.hit", hit)  # đo cache hit rate
    span.set_attribute("source", "redis|local|central")

with tracer.start_as_current_span("sale.commit_transaction") as span:
    span.set_attribute("sale.line_count", len(lines))
    span.set_attribute("sale.payment_count", len(payments))
```

**Quy tắc: không bao giờ đưa PII vào span attribute.** Chỉ `customer_id` (UUID), không bao
giờ tên hay SĐT — traces được lưu và xem rộng rãi hơn dữ liệu nghiệp vụ.

---

## 4. ⚠️ Truyền trace context qua outbox — chi tiết dễ bỏ sót nhất

Đây là điểm quan trọng nhất của cả doc, và là thứ hầu hết triển khai bỏ quên.

**Vấn đề:** đơn hàng chốt lúc 10:00 tại cửa hàng. Sự kiện đồng bộ lên trung tâm lúc 14:00 (sau
khi mạng phục hồi). Mặc định, đó là **hai trace hoàn toàn rời nhau** — không có cách nào lần
từ bản ghi ở trung tâm ngược về giao dịch gốc đã sinh ra nó.

**Giải pháp:** lưu W3C trace context vào chính bản ghi outbox, khôi phục khi xử lý.

```python
# Lúc ghi outbox — trong cùng transaction với đơn hàng
from opentelemetry.propagate import inject

carrier = {}
inject(carrier)  # lấy traceparent hiện tại
outbox_row.payload["_trace"] = carrier

# Lúc sync worker xử lý — có thể là nhiều giờ sau
from opentelemetry.propagate import extract

ctx = extract(outbox_row.payload.get("_trace", {}))
with tracer.start_as_current_span("sync.publish", context=ctx, links=[...]):
    ...
```

Kết quả: một trace liên tục từ **thu ngân bấm nút** → ghi DB → (chờ 4 tiếng) → đẩy lên trung
tâm → ghi ledger trung tâm. Đây chính là thứ cần để trả lời *"vì sao điểm của khách này lên
muộn?"* và *"khâu nào trong chuỗi đồng bộ đang chậm?"*

**Lưu ý:** dùng **span link** thay vì quan hệ cha-con khi độ trễ lớn, vì một span kéo dài 4
tiếng sẽ làm hỏng thống kê thời lượng.

---

## 5. Profiling tầng DB

Tracing nói *"SELECT này mất 300ms"*. Nó không nói **vì sao**.

### PostgreSQL

```sql
-- Bật (postgresql.conf): shared_preload_libraries = 'pg_stat_statements'
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Top truy vấn tốn tổng thời gian nhiều nhất
SELECT calls, round(mean_exec_time::numeric,2) AS avg_ms,
       round(total_exec_time::numeric) AS total_ms, query
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
```

> **Lưu ý đọc kết quả:** sắp xếp theo `total_exec_time`, **không** theo `mean_exec_time`. Một
> truy vấn 5ms gọi 100.000 lần tốn nhiều hơn một truy vấn 2s gọi 10 lần — và nó cũng dễ sửa
> hơn.

Kiểm tra partition pruning có hoạt động:
```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT ... FROM point_ledger WHERE occurred_at >= '2026-09-01';
-- Kỳ vọng: chỉ quét partition của tháng 9, không phải tất cả
```

### ClickHouse

```sql
-- system.query_log bật sẵn
SELECT query_duration_ms, read_rows, formatReadableSize(read_bytes) AS scanned,
       formatReadableSize(memory_usage) AS mem, substring(query, 1, 100)
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 1 HOUR
ORDER BY query_duration_ms DESC LIMIT 20;
```

> **Chỉ số quan trọng nhất là `read_rows`, không phải thời gian.** ClickHouse nhanh; nếu một
> truy vấn chậm thì gần như luôn vì nó **đọc quá nhiều dòng** — tức phân vùng hoặc `ORDER BY`
> key sai, chứ không phải engine yếu.

---

## 6. Lấy mẫu

| Môi trường | Tỷ lệ | Lý do |
|---|---|---|
| Phát triển | 100% | Lưu lượng thấp, muốn thấy hết |
| **Test tải / ngâm** | **100%** | **Đang đi tìm ngoại lệ — lấy mẫu sẽ làm mất đúng cái cần tìm** |
| Production T1 | 10% + 100% cho request lỗi | Cân bằng chi phí |
| Production T2+ | Tail-based sampling qua Collector | Giữ mọi trace chậm/lỗi, bỏ bớt trace bình thường |

**Trong test tải luôn để 100%.** Bottleneck thường nằm ở p99 — lấy mẫu ngẫu nhiên rất dễ bỏ sót.

---

## 7. Dashboard cần dựng

Bốn cái, không hơn — dashboard không ai nhìn là nợ, không phải tài sản.

| # | Dashboard | Nội dung |
|---|---|---|
| D1 | **Sức khỏe cửa hàng** | p95/p99 chốt đơn · cache hit rate · độ sâu outbox · **tuổi sự kiện chưa gửi cũ nhất** · trạng thái circuit breaker |
| D2 | **Đồng bộ** | Độ trễ theo cửa hàng · tỷ lệ thành công · tỷ lệ trùng lặp · cửa hàng lâu không thấy |
| D3 | **Phân rã trace** | Thời gian chốt đơn tách theo giai đoạn — *đây là dashboard săn bottleneck chính* |
| D4 | **Pipeline** | Thời lượng DAG · thời lượng từng dbt model · số dòng theo tầng · kết quả dbt test |

---

## 8. Ảnh hưởng ngân sách & lộ trình

| | |
|---|---|
| RAM khi bật | **+1–1.5 GB** → tổng ~8.5 GB (vẫn vừa máy 16 GB) |
| RAM khi tắt | 0 — chạy bằng compose profile riêng |
| Công sức | ~0.5 ngày cài đặt + ~0.5 ngày dựng dashboard |

**Vào lộ trình:**

| Khi nào | Việc |
|---|---|
| **Ngày 0 (spike)** | Thêm S5: `grafana/otel-lgtm` lên, xác nhận trace từ FastAPI tới được Tempo |
| **Tuần 1** | Auto-instrumentation từ đầu — rẻ hơn nhiều so với gắn thêm sau |
| **Tuần 2** | Truyền trace context qua outbox (§4) — làm cùng lúc viết outbox |
| **Tuần 3** | `astronomer-cosmos` đã cho span mỗi dbt model; bật ClickHouse `query_log` |
| **Tuần 4** | Dashboard D1–D4 · đo overhead · dùng để phân tích kết quả LD-1…LD-4 |

---
*Changelog: 2026-09-11 — tạo mới theo yêu cầu bổ sung stack giám sát.*
