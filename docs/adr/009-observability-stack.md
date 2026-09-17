# ADR-009 — Observability stack: OpenTelemetry + grafana/otel-lgtm

**Trạng thái:** Được chấp nhận · 2026-09-11
**Bối cảnh:** chủ dự án yêu cầu bổ sung stack giám sát (OpenTelemetry) để **tìm bottleneck
khi test**, phù hợp với ưu tiên số 1 là ổn định + scale.

## Bối cảnh

[08-reliability-and-scale.md](../08-reliability-and-scale.md) đặt ra yêu cầu test tải, test
ngâm 72h, test hỗn loạn — nhưng **không có cách nào trả lời câu hỏi quan trọng nhất khi kết
quả xấu: *thời gian đi đâu mất?***

Metrics nói "p95 là 800ms, vượt ngưỡng". Chúng **không** nói 800ms đó nằm ở fsync của
Postgres, ở lời gọi HTTP tới trung tâm, hay ở vòng lặp N+1 khi tải dòng sản phẩm. Với một hệ
thống phân tán qua ranh giới mạng và có xử lý bất đồng bộ, **distributed tracing là công cụ
duy nhất trả lời được**.

## Quyết định

Ba tầng, mỗi tầng trả lời một loại câu hỏi khác nhau:

| Tầng | Công cụ | Trả lời câu hỏi |
|---|---|---|
| **Tracing** | **OpenTelemetry** (auto-instrumentation) | *Thời gian đi đâu?* Xuyên suốt cửa hàng → trung tâm → pipeline |
| **Backend dev/test** | **`grafana/otel-lgtm`** (một container) | Nơi chứa và xem traces/metrics/logs |
| **Profiling DB** | **`pg_stat_statements`** + **ClickHouse `system.query_log`** | *Truy vấn nào chậm, và vì sao?* |

Chạy dưới **compose profile riêng** (`--profile observability`), chỉ bật khi cần.

## Vì sao `grafana/otel-lgtm`

Đây là điểm mấu chốt — ngân sách RAM đã ở ~7 GB trên máy 16 GB.

| Phương án | Container | RAM | Đánh giá |
|---|---:|---:|---|
| **`grafana/otel-lgtm`** ⭐ | **1** | ~1–1.5 GB | OTel Collector + Prometheus + Tempo + Loki + Grafana **trong một image**, cấu hình sẵn, một lệnh chạy |
| Grafana LGTM tách rời | 5 | ~1.5–2 GB | Cùng thành phần nhưng phải tự cấu hình nhiều |
| **SigNoz** | 5 | **≥ 4 GB, khuyến nghị 8 GB** | ❌ **Quá nặng** — riêng nó đã bằng cả phần còn lại của stack. Kéo theo ClickHouse + Keeper + Postgres riêng |
| Jaeger all-in-one | 1 | ~400 MB | Nhẹ nhất nhưng **chỉ có tracing** — phải thêm Prometheus + Grafana riêng cho metrics |

`grafana/otel-lgtm` được thiết kế **đúng cho tình huống này**: môi trường phát triển, demo và
kiểm thử. Grafana nói rõ **không dùng cho production** — và đúng vậy, ta chỉ cần nó khi chạy
test.

**Đường lên production:** đổi endpoint OTLP sang Grafana Cloud, hoặc dựng LGTM tách rời trên
máy riêng, hoặc SigNoz self-host. **Code ứng dụng không đổi một dòng** — đó là điểm mạnh của
OTel: instrumentation độc lập với backend.

## Vì sao OpenTelemetry chứ không phải thư viện riêng của từng vendor

1. **Chuẩn trung lập** — đổi backend không phải sửa code.
2. **Auto-instrumentation phủ gần hết stack của ta**: FastAPI, SQLAlchemy, psycopg, redis,
   httpx đều có sẵn. `opentelemetry-bootstrap` tự phát hiện thư viện đã cài và cài gói
   instrumentation tương ứng.
3. **Ba tín hiệu một mô hình** — traces, metrics, logs dùng chung `trace_id`, tương quan được.
4. Đã xử lý đúng bản chất async của FastAPI, context truyền qua `await`.

Chi phí: overhead nhỏ, phụ thuộc tỷ lệ lấy mẫu. Với hệ thống này (vài trăm request/giây ở
mức cao nhất) là không đáng kể — nhưng **phải đo** (xem §Nghĩa vụ đo overhead).

## Vì sao vẫn cần profiling ở tầng DB

Tracing cho biết *"câu SELECT này mất 300ms"*. Nó **không** cho biết *vì sao* — thiếu index?
quét tuần tự? chờ khóa? bloat?

| Công cụ | Miễn phí? | Trả lời |
|---|---|---|
| `pg_stat_statements` | ✅ Extension chuẩn | Truy vấn nào tốn tổng thời gian nhiều nhất, gọi bao nhiêu lần, trung bình bao lâu |
| `EXPLAIN (ANALYZE, BUFFERS)` | ✅ | Kế hoạch thực thi: có dùng index không, partition pruning có hoạt động không |
| ClickHouse `system.query_log` | ✅ Bật sẵn | Thời gian, số dòng đọc, bytes quét, bộ nhớ dùng — cho **mọi** truy vấn |

Hai cái này bổ sung cho tracing, không thay thế. **Cả hai đều gần như miễn phí** — chỉ cần
bật.

## Nghĩa vụ đo overhead

Instrumentation làm chậm chính hệ thống đang đo. Để số liệu test tải có ý nghĩa:

> **Chạy LD-1 hai lần: một lần bật OTel, một lần tắt. Ghi lại chênh lệch.**

Nếu overhead > 5% thì giảm tỷ lệ lấy mẫu hoặc bỏ bớt span thủ công. Không đo thì không biết
đang tối ưu cái gì.

## Phương án đã xem xét và loại

| Phương án | Vì sao loại |
|---|---|
| **SigNoz** | 4–8 GB RAM. UI đẹp hơn, nhưng bằng cả phần còn lại của stack — vi phạm ràng buộc laptop |
| **Jaeger** riêng lẻ | Chỉ tracing. Phải thêm Prometheus + Grafana → cuối cùng cũng 3 container mà ít tích hợp hơn |
| **Grafana Cloud** (miễn phí tier) | Không chạy được offline, và dữ liệu test đi ra ngoài. Nhưng **là đường lên production tốt** |
| Chỉ dùng metrics Prometheus | Không trả lời được *"thời gian đi đâu"* trong hệ phân tán — đúng câu hỏi cần trả lời |
| Chỉ đọc log | Tương quan thủ công qua `trace_id` là không khả thi khi tìm bottleneck |
| **Pyroscope** (continuous profiling) | Hữu ích cho CPU hotspot, nhưng bottleneck ở hệ này gần như chắc chắn là **I/O và mạng**, không phải CPU. Ghi vào backlog v2 |

## Hệ quả

### Tích cực
- Trả lời được *"thời gian đi đâu"* xuyên cửa hàng → trung tâm → pipeline
- Auto-instrumentation → công sức thấp, phần lớn là cài gói và đặt biến môi trường
- Một container, bật/tắt bằng profile → không ảnh hưởng ngân sách RAM khi không dùng
- Đổi sang backend production không phải sửa code

### Tiêu cực
- +1–1.5 GB RAM **khi bật** (tổng ~8.5 GB, vẫn vừa máy 16 GB)
- Overhead lên chính hệ thống đang đo → phải đo chênh lệch
- `grafana/otel-lgtm` **không dùng được cho production** — cần đổi backend khi lên thật
- Thêm một thứ phải học (~0.5 ngày)

### Việc bắt buộc
- [ ] Truyền **trace context qua outbox** — xem [10-observability.md §4](../10-observability.md)
- [ ] Đo overhead của instrumentation (LD-1 có/không OTel)
- [ ] **Không bao giờ đưa PII vào span attribute** (tên, SĐT) — chỉ `customer_id`
