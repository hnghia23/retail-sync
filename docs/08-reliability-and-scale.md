# 08 — Độ ổn định & Khả năng mở rộng

> **Ưu tiên số 1 của dự án** (chủ dự án xác nhận 2026-09-11): *hệ thống chạy ổn định, khả năng
> scale tốt* — đứng trên độ đầy đủ tính năng.
>
> Doc này biến ưu tiên đó thành yêu cầu kỹ thuật cụ thể và **kế hoạch chứng minh**, thay vì
> chỉ tuyên bố.
>
> 🔀 **Từ 2026-09-23 ([ADR-010](adr/010-data-flow-first.md))**, kế hoạch chứng minh ở §6 là
> toàn bộ **giai đoạn B**, và chạy ngay sau khi luồng dữ liệu (giai đoạn A) thông suốt, không đợi
> tính năng. Nguồn tải là **bộ giả lập** ([18](18-simulator.md)). Điều kiện đạt chung của mọi
> thử nghiệm là `simulator audit` = `CONVERGED` ([17 §5](17-data-flow.md)).
>
> ▶️ **Hiện tại (2026-09-25):** cổng A đạt 2026-09-24, nên **§6 là việc đang làm**. Công cụ đã sẵn:
> bộ giả lập `edge` (3 stack cửa hàng thật, profile `edge-multi`) và `virtual` (20+ cửa hàng ảo,
> tật `offline`/`resend`/`concurrent_customer`), bộ đối soát L0…L4, `tests/scenarios/`. Đã có sẵn
> một phần bằng chứng: AT-02 (mất mạng tới trung tâm), CH-7 (gửi lại lô, 479 lô trong lần chạy
> `virtual`), 2 giờ chạy liên tục 3 cửa hàng với RAM đi ngang
> ([progress/2026-09-24-cong-a.md](progress/2026-09-24-cong-a.md)).

---

## 1. Ổn định nghĩa là gì ở hệ thống này

Không phải "không bao giờ hỏng" — mà là **hỏng có kiểm soát**:

1. **Không mất dữ liệu nghiệp vụ.** Đơn hàng đã chốt và điểm đã tích không bao giờ biến mất,
   dù mất điện, mất mạng, hay app crash.
2. **Bán kính ảnh hưởng bị giới hạn.** Một cửa hàng hỏng không kéo theo cửa hàng khác. Trung
   tâm hỏng không làm cửa hàng ngừng bán.
3. **Tự phục hồi.** Không có nhân viên IT tại cửa hàng — mọi thứ phải tự đứng dậy.
4. **Nhìn thấy được.** Sự cố phải phát ra tín hiệu **trước khi** người dùng gọi điện báo.
5. **Suy giảm có kiểm soát.** Mất thành phần phụ thì chậm hơn, không chết.

---

## 2. Danh mục chế độ hỏng (FMEA)

### 2.1. Tại cửa hàng

| Hỏng | Phát hiện bằng | Phản ứng | Bán kính | Trạng thái |
|---|---|---|---|:---:|
| Trung tâm không tới được | Circuit breaker mở | Dồn outbox, bỏ qua tra cứu khách | 1 CH, suy giảm | ✅ Đã thiết kế |
| Redis chết | Lỗi kết nối | Bỏ qua cache, đọc thẳng PG | 1 CH, chậm hơn | ✅ AT-08 |
| App crash giữa transaction | Tiến trình thoát | Restart, transaction đã rollback | Tức thời | ✅ |
| **Mất điện đột ngột** | — | WAL recovery khi khởi động | Tức thời | ⚠️ **Bắt buộc `synchronous_commit = on`** |
| **Đĩa đầy** | Metric `disk_used_pct` | Cảnh báo ở 75%, dọn log + outbox đã gửi | **1 CH, chí tử** | ⚠️ Cần job dọn + cảnh báo |
| Postgres cửa hàng chết | Healthcheck fail | Restart container; nếu hỏng dữ liệu → khôi phục backup | **1 CH, chí tử** | ⚠️ Cần backup hằng ngày |
| Sync worker chết | Metric `outbox_oldest_unsent_age` tăng | Restart; outbox không mất gì | 1 CH, trễ đồng bộ | ✅ |
| Đồng hồ sai | Trung tâm so `occurred_at` vs `recorded_at` | Cảnh báo nếu lệch > 5 phút | Chất lượng dữ liệu | ⚠️ Cần check |
| **Không tạo được partition tháng mới** | Insert lỗi | — | **Chí tử, toàn hệ thống** | ⚠️ **Xem §4.1** |

### 2.2. Tại trung tâm

| Hỏng | Phát hiện bằng | Phản ứng | Bán kính | Trạng thái |
|---|---|---|---|:---:|
| Cạn kết nối Postgres | Metric `pg_connections_used` | PgBouncer; trả 503 + `Retry-After` | Mọi CH, chậm đồng bộ | ⚠️ Cần giới hạn + 503 tử tế |
| **Dội ngược sau sự cố mạng diện rộng** | Metric tốc độ request tăng vọt | Rate limit theo cửa hàng + backoff có jitter | Mọi CH | ⚠️ Cần rate limit |
| Ledger lệch snapshot | Job đối soát hằng ngày | Cảnh báo; dựng lại snapshot từ ledger | Sai số điểm | ✅ AT-10 |
| Khách trùng do 2 CH cùng offline | Job quét trùng `phone_hash` | Gộp `customer_id` trong ledger | Chất lượng dữ liệu | ⚠️ Cần job |
| Đĩa đầy | Metric | Cảnh báo; phân vùng cũ chuyển sang lưu trữ lạnh | Mọi CH | ⚠️ |

### 2.3. Nền tảng dữ liệu

Mọi hỏng hóc ở đây **không được ảnh hưởng vận hành** — chỉ làm báo cáo cũ đi.

| Hỏng | Phản ứng |
|---|---|
| DAG lỗi | Airflow retry; backfill lại phân vùng ngày đó |
| Warehouse lệch lake | Kiểm tra số dòng theo phân vùng → dựng lại từ bronze |
| dbt test đỏ | Chặn model hạ nguồn, cảnh báo, **không** đẩy dữ liệu sai xuống BI |

---

## 3. Backpressure & giới hạn tài nguyên

### 3.1. Outbox có tràn không?

Tính thử trường hợp xấu — cửa hàng T3 mất mạng **30 ngày**:

```
800 đơn/ngày × 3 sự kiện/đơn × 30 ngày = 72 000 dòng
72 000 × ~1 KB payload                  ≈ 72 MB
```

**Kết luận: outbox không phải rủi ro tràn ở cửa hàng.** Kể cả offline một tháng vẫn chỉ vài
chục MB. Rủi ro thật là **đĩa đầy vì log và dữ liệu lịch sử**, không phải outbox.

Nhưng vẫn cần: job dọn dòng `sent_at` cũ hơn 7 ngày, và metric cảnh báo.

### 3.2. Backpressure ở trung tâm

Đây **mới là** chỗ cần backpressure. Khi 200 cửa hàng cùng phục hồi sau sự cố:

| Cơ chế | Cấu hình |
|---|---|
| Giới hạn kích thước lô | 100–500 sự kiện/request |
| Rate limit theo cửa hàng | Ví dụ 10 req/giây/CH; vượt → `429` + `Retry-After` |
| Backoff có jitter ở worker | `min(2^n, 300)` giây × random(0.5–1.5) — jitter là bắt buộc, không có thì mọi CH retry đồng pha |
| Giới hạn concurrency ở ingest | Semaphore; vượt → `503` + `Retry-After` |

**Nguyên tắc:** trung tâm **từ chối tử tế** thay vì sập. Cửa hàng đã có outbox nên bị từ chối
không mất gì — chỉ chậm hơn.

---

## 4. Yêu cầu về khả năng mở rộng

### 4.1. ⚠️ Tự động tạo partition — rủi ro sập cao nhất, dễ quên nhất

`point_ledger` phân vùng theo tháng. **Postgres không tự tạo partition tương lai.** Nếu đến
ngày 1 tháng sau mà partition chưa tồn tại → **mọi INSERT đều lỗi** → toàn hệ thống điểm chết.

Đây là sự cố production kinh điển. Bắt buộc:

- Dùng **`pg_partman`**, hoặc một job tạo trước **3 tháng** partition, **và giữ tháng trước**
  (cửa hàng offline qua cuối tháng đồng bộ ngày 1 — ở tháng go-live partition đó không có sẵn;
  migration `0006`, phát hiện 2026-09-24 bằng bộ giả lập `virtual`)
- **Kiểm tra hằng ngày**: partition của tháng sau đã tồn tại chưa → cảnh báo nếu chưa
- Test: chỉnh đồng hồ tới tháng sau, xác nhận insert vẫn chạy

### 4.2. Job đối soát phải tăng dần, không quét toàn bộ

Job đối soát (AT-10) so `point_balance` với `SUM(point_ledger)`. Ở T3 đó là **584 triệu dòng
mỗi năm** — quét toàn bộ mỗi ngày là không khả thi.

Thiết kế đúng ngay từ đầu:

```sql
-- CHỈ đối soát khách có biến động kể từ lần chạy trước
WHERE customer_id IN (
    SELECT DISTINCT customer_id FROM point_ledger
    WHERE recorded_at > :last_reconcile_at
)
-- Cộng thêm: mỗi tháng quét toàn bộ một lần vào giờ thấp điểm
```

### 4.3. Cập nhật snapshot phải tăng dần

`point_balance` cập nhật theo sự kiện đến, không tính lại từ đầu. Tính lại toàn bộ chỉ dùng
khi khôi phục sự cố.

### 4.4. Bảng kiểm chứng scale seam

Mỗi seam trong [02 §4](02-scale-capacity.md) phải **được chứng minh trước khi cần**, không
phải lúc đang cháy:

| Seam | Cách chứng minh | Khi nào |
|---|---|---|
| Phân vùng ledger | Nạp 50 triệu dòng (bộ giả lập `bulk`), đo truy vấn có/không partition pruning | Giai đoạn B |
| PgBouncer | Mô phỏng 200 kết nối đồng thời | Giai đoạn B |
| Backfill warehouse | Xóa 1 tháng, dựng lại từ bronze, so số dòng | Giai đoạn A (cổng A) |
| Đồng bộ nhiều CH | Bộ giả lập `virtual`: 20 → 200 cửa hàng ảo cùng đẩy | Giai đoạn A (20) · B (200) |
| Ngưỡng ClickHouse | Nạp dữ liệu T2 (~256 Tr dòng, bộ giả lập `bulk`), đo truy vấn lớp C | Giai đoạn B |

---

## 5. Quan sát được — tối thiểu bắt buộc

Ưu tiên "ổn định" nâng observability từ *nếu kịp* lên **Must**. Không thấy thì không vận hành
được.

> 📍 **Phần này chỉ liệt kê *chỉ số* và *ngưỡng cảnh báo*.** Stack công cụ (OpenTelemetry +
> `grafana/otel-lgtm`), cách instrument, và phương pháp **săn bottleneck** nằm ở
> [10-observability.md](10-observability.md) — quyết định chọn stack ở
> [ADR-009](adr/009-observability-stack.md).
>
> Khác biệt vai trò: **metrics nói *có vấn đề*, traces nói *vấn đề ở đâu*.** Cần cả hai.

> ✅ **Hiện thực (2026-09-25):** các chỉ số dưới đây là metric Prometheus thật trên dashboard
> "Sức khỏe luồng dữ liệu" — tên metric và nơi phát ở [17 §6](17-data-flow.md). Chỉ số dạng tỉ
> lệ (`sync_failure_rate`, `event_duplicate_rate`) là biểu thức PromQL trên counter
> (`sync_push_failures_total`, `ingest_events_total{outcome="duplicate"}`). **Chưa có alert rule**
> (roadmap ngày 19): ngưỡng mới được vẽ trên dashboard.

### Chỉ số tại cửa hàng

| Chỉ số | Ngưỡng cảnh báo | Vì sao |
|---|---|---|
| `outbox_depth` | > 5 000 | Đồng bộ đang tắc |
| **`outbox_oldest_unsent_age_seconds`** | **> 3 600** | **Chỉ số sức khỏe quan trọng nhất** — nói lên dữ liệu đang trễ bao lâu |
| `sale_commit_duration_p95` | > 500 ms | Vi phạm NFR-02 |
| `sync_failure_rate` | > 10% trong 5 phút | Mạng hoặc trung tâm có vấn đề |
| `disk_used_pct` | > 75% | Nguy cơ chí tử |
| `db_connections_used` | > 80% pool | Sắp cạn |
| `circuit_breaker_state` | mở > 10 phút | Mất kết nối trung tâm kéo dài |

### Chỉ số tại trung tâm

| Chỉ số | Ngưỡng | Vì sao |
|---|---|---|
| `store_sync_lag_seconds{store}` | > 900 | Cửa hàng nào đang trễ (FR-C10) |
| `stores_not_seen_recently` | ≥ 1 quá 1 giờ | Cửa hàng có thể đã chết |
| `event_duplicate_rate` | — | Xác nhận idempotency đang hoạt động |
| `reconcile_drift_count` | **> 0** | **Sai số điểm — nghiêm trọng nhất** |
| `pg_connections_used` | > 70% | Cần PgBouncer |

### Log & truy vết

- Log JSON có cấu trúc (`structlog`), **không** log chuỗi tự do
- `trace_id` sinh ở cửa hàng, đi theo sự kiện lên trung tâm và vào cả log warehouse
- **Không bao giờ log PII** (tên, SĐT) — chỉ log `customer_id`
- **Trace context phải truyền qua bảng `outbox`** để nối được giao dịch gốc với lần đồng bộ
  xảy ra hàng giờ sau — xem [10-observability.md §4](10-observability.md)

### Profiling tầng DB *(bổ sung 2026-09-11)*

Metrics và traces không nói được *vì sao* một truy vấn chậm. Bật thêm — cả hai gần như miễn phí:

- **Postgres:** `pg_stat_statements` + `EXPLAIN (ANALYZE, BUFFERS)` để kiểm chứng partition pruning
- **ClickHouse:** `system.query_log` (bật sẵn) — chỉ số quan trọng nhất là `read_rows`, không
  phải thời gian

---

## 6. Kế hoạch chứng minh — cách *chứng minh* thay vì *tuyên bố*

Đây là phần biến ưu tiên thành bằng chứng.

### 6.1. Test hỗn loạn (chaos) — Must

| # | Thử nghiệm | Đạt khi |
|---|---|---|
| CH-1 | `docker network disconnect` trung tâm 30 phút khi đang bán | 0 đơn mất; hội tụ < 5 phút sau khi nối lại |
| CH-2 | `kill -9` app giữa lúc chốt đơn, lặp 20 lần | Không có đơn nửa vời; `SUM(sale_payment) = sale.total` luôn đúng |
| CH-3 | `kill -9` Postgres cửa hàng (mô phỏng mất điện) | Sau khởi động lại: 0 giao dịch đã commit bị mất |
| CH-4 | Làm đầy đĩa tới 95% | Cảnh báo phát ra **trước** khi ghi lỗi |
| CH-5 | Tắt Redis khi đang bán | Bán bình thường, chỉ chậm hơn |
| CH-6 | Ngắt mạng 10 cửa hàng rồi nối lại **cùng lúc** | Trung tâm không sập; rate limit hoạt động; mọi sự kiện tới đủ |
| CH-7 | Gửi lặp cùng một lô sự kiện 10 lần | Điểm cộng đúng một lần |

### 6.2. Test ngâm (soak) — Must

Chạy **72 giờ liên tục** với tải mô phỏng đều đặn, theo dõi:

- Bộ nhớ có rò rỉ không (RSS của app)
- **Bloat bảng `outbox`** — đây là lý do phải chỉnh autovacuum riêng
- Số kết nối có rò rỉ không
- Độ trễ p95 có trôi dần không
- Đĩa tăng có đúng dự đoán không

Đây là test duy nhất bắt được lỗi mà test chức năng không bắt được: **suy thoái theo thời gian**.

> 🔍 **Mọi thử nghiệm dưới đây chạy với OpenTelemetry bật ở mức lấy mẫu 100%.** Kết quả xấu
> mà không có trace thì chỉ biết *có vấn đề*, không biết *ở đâu*. Xem
> [10-observability.md §2](10-observability.md) để biết bottleneck nào đang bị nghi ngờ.

### 6.3. Test tải (load) — Must

Công cụ: **bộ giả lập**, phát tải vòng hở ([18 §1, §9](18-simulator.md)). Không dùng `k6`
(đổi 2026-09-23). Phép đo chỉ hợp lệ khi bộ giả lập < 50% CPU.

| # | Kịch bản | Mục tiêu |
|---|---|---|
| LD-1 | 1 cửa hàng, tải đỉnh giờ cao điểm | p95 < 500 ms |
| LD-2 | 10 → 200 cửa hàng ảo đồng thời đẩy đồng bộ | Trung tâm giữ p95 < 1 s |
| LD-3 | Nạp dữ liệu T2 (256 Tr dòng fact) vào warehouse | Truy vấn lớp C < 3 s |
| LD-4 | 200 kết nối đồng thời tới Postgres trung tâm | Không lỗi; hoặc 503 tử tế |

### 6.4. Test toàn vẹn dữ liệu — Must

| # | Kiểm tra | Tần suất |
|---|---|---|
| DI-1 | `point_balance.balance = SUM(point_ledger.delta)` | Hằng ngày (AT-10) |
| DI-2 | `sale.total = SUM(sale_line.line_total) − chiết khấu` | CHECK constraint, liên tục |
| DI-3 | `SUM(sale_payment.amount) = sale.total` | CHECK/trigger, liên tục |
| DI-4 | Số dòng trong warehouse = số dòng trong bronze theo phân vùng | Mỗi lần chạy DAG |
| DI-5 | Mọi `sale` ở cửa hàng đều có mặt ở trung tâm sau 1 giờ | Hằng ngày |

DI-1…DI-5 là **các ô** của bảng đối soát xuyên tầng ở [17 §5](17-data-flow.md). Bộ đối soát
kiểm tất cả trong một lần chạy, thêm một tầng mà bảng này chưa có: **manifest của bộ giả lập**,
tức đáp án đúng.

---

## 7. Hệ quả lên lộ trình

Ưu tiên "ổn định + scale" **thay đổi thứ tự ưu tiên**, không chỉ thêm việc:

| Hạng mục | Trước | Sau |
|---|---|---|
| Observability (metrics, log có cấu trúc, cảnh báo) | *Nếu kịp* | 🔴 **Must** |
| **Distributed tracing (OpenTelemetry)** 🆕 | Không có | 🔴 **Must** — [ADR-009](adr/009-observability-stack.md) |
| **Profiling DB** (`pg_stat_statements`, `query_log`) 🆕 | Không có | 🔴 **Must** — gần như miễn phí |
| Test hỗn loạn | Should | 🔴 **Must** |
| Test ngâm 72h | Không có | 🔴 **Must** |
| Test tải | Should | 🔴 **Must** |
| Tự động tạo partition | Không có | 🔴 **Must** |
| Backpressure + rate limit | Không có | 🔴 **Must** |
| Backup + quy trình khôi phục cửa hàng | Không có | 🔴 **Must** |
| Đối soát tăng dần | Ngụ ý | 🔴 **Must** |
| — | | |
| Trừ tồn kho | Should | 🟡 Could |
| Chốt ca đầy đủ (đối soát tiền) | Should | 🟡 Could *(giữ thực thể `shift`, bỏ phần đối soát)* |
| SCD Type 2 | Should | 🟡 Could |
| Metabase | Should | 🟡 Could *(thay bằng SQL mẫu)* |
| Theo dõi sync theo cửa hàng | Should | 🔴 **Must** *(là observability)* |

**Đánh đổi:** ít tính năng hơn, nhiều bằng chứng hơn. Đúng với ưu tiên đã nêu.

> **Bước tiếp theo của cùng hướng này (2026-09-23, [ADR-010](adr/010-data-flow-first.md)):**
> không chỉ hạ hạng tính năng phụ, mà **dời mọi tính năng dùng tại quầy** ra sau cổng B. Bằng
> chứng không còn bị dồn về tuần cuối nữa: nó bắt đầu ngay khi luồng dữ liệu thông suốt.

---
*Changelog: 2026-09-25 — ghi trạng thái hiện tại ở đầu doc (cổng A đạt, §6 đang làm).*

*Changelog: 2026-09-23 — theo ADR-010: §6 là giai đoạn B, nguồn tải là bộ giả lập (thay `k6`),
điều kiện đạt là `audit` = `CONVERGED`; lịch §4.4 đổi từ tuần sang giai đoạn.*

*Changelog: 2026-09-11 — tạo mới theo ưu tiên "ổn định + scale" của chủ dự án.*
