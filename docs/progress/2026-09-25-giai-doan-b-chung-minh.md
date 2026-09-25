# Tiến độ 2026-09-25 (lần 4) — Giai đoạn B: hỗn loạn CH-1…7, diễn tập cảnh báo, dữ liệu T2

> Nhật ký tiến độ, không phải docs thiết kế. Nối tiếp
> [2026-09-25-giai-doan-b-van-hanh.md](2026-09-25-giai-doan-b-van-hanh.md).

Chủ dự án yêu cầu **hoàn thành giai đoạn B trước khi chạy test ngâm 72h**, rồi cho **tạm dừng**
giữa chừng để cập nhật docs. Nhật ký này ghi đúng điểm dừng: phần nào đã chạy thật và đạt, phần nào
mới có công cụ, phần nào chưa chạy.

Bằng chứng thô (gitignore) ở `runs/chaos/20260925T090931Z/CH-*.json`,
`runs/alerts/{timeline,sink-part1,sink-part2}.jsonl`, `runs/alerts/*/drill-*.json`,
`runs/bulk-t2-24m-s42/bulk.json`.

## 1. Tóm tắt

| Hạng mục cổng B | Trạng thái |
|---|---|
| **CH-1…CH-7** | ✅ **cả 7 đạt** trên compose thật, mọi kịch bản kết thúc `CONVERGED` |
| Mọi ngưỡng cảnh báo cấu hình + kích hoạt thử | 🟡 **21 rule** (docs/08 §5 đủ), **16/21** đã kêu thật VÀ tới `alert-sink` |
| Partition tháng sau tự tồn tại | ✅ test "chỉnh đồng hồ" bằng libfaketime: 14 lần sang tháng |
| DI-1…DI-5 | 🟡 DI-1…DI-3 trong mọi lần `audit` của CH-1…7 (L0→L2); DI-4/DI-5 cần một lần audit tới L3/L4 sau khi DAG bắt kịp |
| LD-1…LD-4, overhead OTel | ⏳ công cụ xong (`tests/scenarios/test_load.py`), **chưa chạy** |
| Dữ liệu T2 trong ClickHouse, lớp C < 3 s | ⏳ dữ liệu T2 **đã sinh** (79 triệu đơn, trên lake), chưa nạp/đo |
| 50 triệu dòng ledger + EXPLAIN | ⏳ dữ liệu **đã sinh** (50,1 triệu dòng), chưa nạp/đo |
| Số đo thật vào docs/02 | ⏳ chờ LD |
| `git clone` → chạy theo README | ⏳ |
| Test ngâm 72h | ⏸ theo yêu cầu: sau khi xong B |

## 2. Code mới (commit `b4d5791`)

**Hệ thống**
- **Rate limit theo cửa hàng** cho `POST /events` (token bucket 10 req/s, dồn 20 → `429` +
  `Retry-After`, `central.ingest.ratelimit`) — docs/08 §3.2 có từ đầu, code chưa có.
- **Heartbeat**: worker rảnh gửi lô rỗng mỗi 5 phút; trung tâm ghi "còn sống, outbox trống" (`updated_at`,
  `lag_seconds = 0`). Không có nó thì cửa hàng không bán gì trông y hệt cửa hàng chết, và worker rảnh
  không bao giờ phát hiện trung tâm mất.
- **Trần backoff 300 → 240 s**: jitter toàn phần nên lần ngủ cuối dài bằng trần; NFR-04 đòi hội tụ
  < 5 phút sau khi nối lại.
- Metric còn thiếu so với docs/08 §5: `disk_used_ratio`, `db_pool_used_ratio` (edge + central),
  `pg_connections_used_ratio`, `reconcile_full_last_run_age_seconds`, nhãn `watched` cho cửa hàng im lặng.
- **21 rule cảnh báo** (15 → 21) + contact point webhook → service **`alert-sink`** (ghi mọi thông báo,
  `GET :8089/alerts`). Script sinh dashboard/cảnh báo chuyển VÀO repo (`infra/observability/build_*.py`)
  — trước đó file ghi "sinh từ script" mà script nằm ngoài repo.

**Bộ giả lập**: chế độ **`bulk`** (docs/18 §3, prefix lake riêng `bronze/bulk-<profile>`, nạp bằng bộ
nạp thật `pipeline load --until`), profile **t2/t3**, `resend_times`, `5xx` ở chốt đơn = "không rõ kết
cục", mở/đóng ca thử lại khi cửa hàng tạm chết.

**Công cụ chứng minh**: `tests/scenarios/test_chaos.py` (CH-1…7), `test_alert_drills.py`,
`test_load.py` (LD-1/2/4 + OTel), `infra/loadtest/ld3_warehouse.py`, `infra/loadtest/seam_ledger.py`,
`tests/integration/test_partition_clock.py`, `test_bulk_schema.py`, `infra/alert_watch.py`. Target
Makefile: `test-chaos`, `test-alert-drills`, `test-load`, `alert-watch`, `ld3`, `seam-ledger`, `sim-bulk`.

## 3. Phát hiện — những thứ sai mà chỉ lộ ra khi đo thật

| # | Phát hiện | Mức | Đã làm |
|---|---|---|---|
| 1 | **Quét toàn bộ INV-4 không ai lên lịch.** Lệnh `reconcile --full` ("mỗi tháng, giờ thấp điểm", docs/08 §4.2) có từ đầu; lượt tăng dần cố ý bỏ qua khách không còn giao dịch, nên snapshot hỏng của họ không bao giờ bị phát hiện. Cùng loại lỗi với hàm tạo partition | 🔴 | `central-maintenance` tự quét toàn bộ mỗi 30 ngày trong 01–05 giờ cửa hàng (`FullScanPolicy`), mốc riêng `point_balance_vs_ledger:full`, cảnh báo `rs-reconcile-full` (> 35 ngày, không có dữ liệu cũng kêu) |
| 2 | **dbt tốn O(toàn bộ lịch sử) mỗi giờ.** 3 fact đọc view staging khử trùng CẢ bronze rồi mới lọc tháng; `affected_months()` và cả 5 dim làm lại việc đó; DAG chạy test sau mỗi model — `GROUP BY` trên toàn fact mỗi giờ. Ở T2 là sắp xếp 79 triệu đơn nhiều lần mỗi giờ | 🔴 | Fact đọc THẲNG bronze, điều kiện tháng đẩy xuống trước khi khử trùng + cắt phân vùng `_dt` theo nhật ký nạp (`macros/incremental.sql`); dim lấy tập khóa bằng `DISTINCT` một cột; test trên fact theo cửa sổ `test_window_days` (35 ngày; toàn bộ bằng `--vars`); `rebuild_months` để dựng lại từng tháng. **Đúng đã kiểm** (54/54 test dbt trên dữ liệu bulk, 7/7 `test_dbt_marts`); **nhanh chưa đo ở T2** — việc của LD-3 |
| 3 | Điều kiện cửa sổ `today() - 36500` **tràn kiểu `Date`/`DateTime`** của ClickHouse (ra năm 2062/2106) → test "toàn bộ lịch sử" loại HẾT dòng và xanh mà không kiểm gì | 🔴 (của chính bản sửa #2) | `toDate32` / `now64()`. Bắt được nhờ `test_duplicate_version_in_bronze_fails_the_build` đòi build đỏ |
| 4 | **Trễ đồng bộ đứng mãi** sau một lần xả tồn đọng: `lag_seconds` là trễ của lô CUỐI, không ai đặt lại khi cửa hàng đã bắt kịp — `rs-store-lag` kêu vĩnh viễn vì cửa hàng ảo của các lần chạy trước | 🟠 | Heartbeat (= outbox trống) đặt `lag_seconds = 0`; cửa hàng ảo gửi heartbeat cuối khi xả xong |
| 5 | Bộ giả lập xếp `5xx` ở `POST /sales` vào "bị từ chối" — máy chủ chết giữa lúc commit thì đơn CÓ THỂ đã ghi → bộ đối soát sẽ báo "đơn thừa" và DIVERGED giả ở CH-2/CH-3 | 🟠 | `5xx` = "không rõ kết cục" (chỉ `4xx` là chắc chắn chưa ghi) |
| 6 | Webhook Grafana **không mang uid** của rule trong nhãn → test kiểm "thông báo đã tới" trượt dù thông báo đã tới (CH-4 lần 1) | 🟡 | `alert-sink` lấy uid từ `generatorURL`; harness khớp cả tiêu đề |
| 7 | `docker update --cpus 0` **không gỡ** giới hạn CPU → sau diễn tập "DB chậm", DB store-002 vẫn bị bóp 5% CPU | 🟡 | Gỡ tay; harness đặt lại bằng đúng số CPU của máy ảo Docker |
| 8 | Fixture dbt của test tích hợp không trung thực: `_dt` cố định và nhật ký nạp chỉ một cửa sổ 1 giờ — trái bất biến mà pipeline thật luôn giữ | 🟡 | `_dt` = ngày của `recorded_at`, nhật ký nạp liền mạch như S4 |

## 4. Test hỗn loạn — cả 7 đạt

Stack dev 3 cửa hàng + trung tâm + data + observability. Tiêu chí chung: `audit` về `CONVERGED`.

| # | Kịch bản | Kết quả |
|---|---|---|
| CH-1 | `docker network disconnect` trung tâm **30,4 phút** khi 3 cửa hàng đang bán | 270/270 đơn `201` (cửa hàng tự chủ), `attempts` = 0 suốt, trong lúc mất mạng `CONVERGING` (chưa tới ≠ mất), **hội tụ 168 s** sau khi nối lại (< 5 phút). `rs-sync-failure-rate` kêu sau 5 phút, `rs-circuit-open` sau 10 phút |
| CH-2 | `kill -9` Edge API giữa lúc chốt đơn, **20 lần** trong 4 phút (~2,5 đơn/s) | **0 đơn nửa vời** (Σ thanh toán = tổng, Σ dòng − chiết khấu = tổng); 178 đơn `201`, 149 đơn "không rõ kết cục" — mỗi đơn có ở mọi tầng hoặc vắng ở mọi tầng; khởi động lại 6–33 s |
| CH-3 | `kill -9` Postgres cửa hàng **3 lần** giữa giờ bán | Log xác nhận mất điện thật (*"not properly shut down; automatic recovery"*, redo 0,07 s); **0 giao dịch đã commit bị mất** (236 đơn `201` đều còn), 24 không rõ kết cục đều nhất quán; phục hồi 8–19 s |
| CH-4 | Đĩa của cửa hàng thử (tmpfs 512 MB, không đụng đĩa thật) làm đầy tới **95%** | `rs-store-disk` (> 75%) **kêu sau 93 s và tới `alert-sink`**, cửa hàng vẫn bán 30/30 đơn ở 95% (cảnh báo tới TRƯỚC lần ghi lỗi); gỡ tải → hết kêu sau 30 s |
| CH-5 | Tắt Redis khi đang bán | 40/40 đơn, p95 chốt đơn 243 → 267 ms; `/health` báo `redis: degraded`. Đường bán không phụ thuộc Redis |
| CH-6 | 10 cửa hàng ảo (T2) mất mạng CÙNG LÚC rồi nối lại CÙNG LÚC, **16.096 sự kiện** tồn đọng | `/health` trung tâm **0 lần hỏng** (873 lần hỏi, chậm nhất 2,35 s), backpressure hoạt động (3 lô `503`), 0 dead-letter, `CONVERGED` |
| CH-7 | Gửi lặp một lô thật (49 sự kiện từ outbox) **10 lần**; thêm 3 cửa hàng ảo gửi lại 30% số lô × 10 | Nhận đủ cả 10 lần; sổ cái / số dư / số đơn không đổi (17 / 1.164 / 20). Ảo: 170 lô gửi lại, 0 lệch |

**Một con số cần theo dõi (CH-6):** lúc dồn, `POST /events` phía client p50 **5,6 s**, p95 **9,3 s** — sát
timeout 10 s của worker; 91/94 lỗi đường truyền là timeout chứ không phải `503`. Lúc đó máy đang chạy
song song bộ sinh T2 (4 tiến trình) nên chưa tách được đâu là giới hạn của trung tâm. **LD-2 phải đo lại
khi máy rảnh**; nếu vẫn vậy thì semaphore 8 + lô 200 cần chỉnh (lô nhỏ hơn khi xả, hoặc `503` sớm hơn).

## 5. Cảnh báo — 16/21 đã kích hoạt thử

Kích hoạt thử = Grafana đưa rule sang `firing` **và** `alert-sink` nhận thông báo (`infra/alert_watch.py
--report`). Không hạ ngưỡng, không sửa rule — tạo điều kiện thật.

| Rule | Kích hoạt bằng |
|---|---|
| `rs-sync-failure-rate`, `rs-circuit-open` | CH-1 |
| `rs-store-disk` | CH-4 |
| `rs-outbox-depth`, `rs-outbox-oldest`, `rs-store-silent` | Diễn tập store-003: worker **chết 67 phút** trong khi quầy vẫn bán (5.586 sự kiện tồn đọng) → kêu đủ ba; bật lại worker → **xả hết trong 37 s**, `CONVERGED` |
| `rs-drift`, `rs-reconcile-full` | Làm hỏng số dư một khách → quét toàn bộ phát hiện; sửa lại → lệch đóng |
| `rs-audit-diverged` | Đối soát một manifest có đơn ma |
| `rs-dead-letter` | Sự kiện `schema_version` 99 thật sự đi qua `POST /events` |
| `rs-partition` | Xóa 2 partition tương lai → kêu; một lượt `central.ops.maintenance` → hết |
| `rs-horizon` | Giữ transaction mở 12 phút → kêu sau 5 + 5 phút ⚠️ test vẫn đỏ ở bước sau khi kêu — chưa xem được traceback (dừng giữa chừng), **cần chạy lại** |
| `rs-monitor` | MinIO chết 7 phút |
| `rs-sales-p95` | DB store-002 bóp còn 5% CPU dưới tải (diễn tập bị dừng giữa chừng, rule đã kêu) |
| `rs-ingest-p95`, `rs-store-lag` | Kêu TỰ NHIÊN, không có chủ đích: p95 `POST /events` > 1 s lúc CH-2/CH-3 chạy cùng bộ sinh T2; trễ đồng bộ cũ của cửa hàng ảo (phát hiện #4). Cần kích hoạt lại có chủ đích (diễn tập "DB trung tâm chậm") |

**Chưa kêu (5):** `rs-db-pool` (diễn tập DB chậm bị dừng trước 5 phút giữ), `rs-central-pg-conn` (LD-4),
`rs-loaded-until`, `rs-transform`, `rs-maintenance` (cần Airflow / bảo trì dừng nhiều giờ — ghép vào đợt
đo LD-3 dài).

## 6. Partition "chỉnh đồng hồ" (docs/08 §4.1) — xong

`tests/integration/test_partition_clock.py`: Postgres chạy với **libfaketime** (image
`tests/fixtures/faketime`), nên `now()` của DB — thứ hàm tạo partition dựa vào — bị dời sang từng đầu
tháng trong **14 tháng** tới, mỗi lần một lượt `central.ops.maintenance` rồi hai cửa hàng gửi điểm (một
bán lúc 00:01, một offline từ tối hôm trước → điểm thuộc tháng trước). Cả 28 lần nhận đúng partition.
Đối chứng: dời đồng hồ qua partition cuối mà KHÔNG bảo trì → sự kiện điểm bị từ chối vĩnh viễn (đúng
tình trạng trước 2026-09-25); một lượt bảo trì là hồi phục. Mẹo: libfaketime phải nạp CHỈ cho tiến trình
`postgres` (nạp cho cả script khởi tạo của image thì nó treo).

## 7. Dữ liệu cho LD-3 và seam ledger — đã sinh, chưa đo

| | T2 24 tháng (2024-09 → 2026-08) | Thêm 3 tháng (2024-06 → 08) |
|---|---|---|
| Cửa hàng | 200 (cửa hàng thật đầu tiên của master data) | 200 |
| Đơn / dòng hàng | **79,3 triệu / 262,9 triệu** | 10,0 triệu / 33,2 triệu |
| Sổ cái điểm | 44,5 triệu | 5,6 triệu → **tổng 50,1 triệu** |
| Khách | 2,38 triệu | 0,30 triệu |
| Doanh thu | 42.353 tỷ đ | 5.344 tỷ đ |
| Thời gian sinh | 58 phút (4 tiến trình, máy bận) + ghi lake 17 phút | 6 phút |
| Chỗ nằm | lake `bronze/bulk-t2` (13 GB) + mảnh cục bộ `runs/bulk-t2-24m-s42/work` (13 GB) | `runs/bulk-t2-extra-s43/work` (1,5 GB) |

Kiểm trên bản nhỏ (3 cửa hàng × 2 tháng, `dw_bulk_smoke`): nạp thật + dbt thật, mart khớp đáp án của bộ
sinh **tới từng đồng** (54.618.630.004 đ, 101.754 đơn, 336.936 dòng, 56.564 điểm).

## 8. Điểm dừng và việc còn lại (theo thứ tự)

Trạng thái lúc dừng: stack dev chạy đủ, 4 API `200`, mọi thứ bị gây sự cố đã khôi phục (mạng trung tâm,
worker store-003, MinIO, CPU DB store-002); không còn test nào chạy nền. Đĩa C: còn 52 GB.

1. Chạy lại diễn tập `rs-horizon`; chạy diễn tập **DB trung tâm chậm** (`rs-ingest-p95`, `rs-store-lag`
   có chủ đích) và DB cửa hàng chậm đủ 16 phút (`rs-db-pool`).
2. **LD-1 + overhead OTel, LD-2 (10 → 200 cửa hàng ảo), LD-4** — khi máy RẢNH (không chạy song song gì).
   LD-4 kích hoạt `rs-central-pg-conn`. LD-2 trả lời câu hỏi p95 9,3 s của CH-6.
3. **LD-3**: đo bản dbt CŨ vài tháng đầu (`--dbt-dir` trỏ vào bản xuất từ commit `9d13cb0`) để có số
   "trước", rồi bản mới đủ 24 tháng + bộ truy vấn lớp C. Trong lúc đó dừng Airflow + `central-maintenance`
   nhiều giờ → kích hoạt `rs-loaded-until`, `rs-transform`, `rs-maintenance`.
4. **Seam 50 triệu dòng ledger** (`make seam-ledger WORK="runs/bulk-t2-24m-s42/work runs/bulk-t2-extra-s43/work"`).
5. `store_backup.py restore` thật; `git clone` → README; ghi số đo vào docs/02; cập nhật cổng B.
6. Test ngâm 72h.

Dọn khi xong: database `dw_bulk_smoke`, prefix `bronze/bulk-smoke`, `runs/bulk-*` (~28 GB), image
`retail-sync-ci-*` / `central-reconcile` cũ.
