# Giai đoạn B — bằng chứng chạy trên máy CI dùng một lần (2026-09-28)

> Tiếp nối [2026-09-25-giai-doan-b-chung-minh.md](2026-09-25-giai-doan-b-chung-minh.md) §8. Chủ dự án
> không muốn máy cá nhân chạy liên tục nhiều giờ. Phần việc còn lại **chạy được dưới 6 giờ** chuyển
> sang GitHub Actions (repo public: runner chuẩn 4 vCPU / 16 GB, miễn phí). Phần không chạy được ở đây
> (test ngâm 72h, LD-3, seam 50 triệu dòng ledger) chờ một VPS.

## 1. CI đỏ vì image MinIO — đã sửa, CI xanh

`quay.io/minio/minio` bắt đăng nhập (job `integration`: *unauthorized*). Máy dev vẫn chạy chỉ nhờ image
đã nằm trong cache. Chuyển sang bản build của Chainguard, ghim digest (lý do và cách cập nhật:
[ADR-005 §Rủi ro nguồn cung](../adr/005-clickhouse-warehouse.md)). Commit `f85edac`. **Cả 4 job xanh**
(`quality`, `unit`, `integration`, `scenarios`). Như vậy `scenarios` đã kiểm lại "`git clone` → chạy
được" trên máy không có cache image.

## 2. Workflow `proof` (`.github/workflows/proof.yml`)

Chỉ chạy bằng tay: Actions → **proof** → Run workflow → chọn nhóm (+ `-k` tùy chọn để chạy lại một
test). Mỗi nhóm chạy trên **một runner riêng, song song**. Runner dựng stack bằng
`infra/bootstrap.py --observability`, ghi nhật ký cảnh báo ở nền (`infra/alert_watch.py`), chạy test,
rồi hủy máy.

| Nhóm (`all` = 6 máy) | Chạy gì | Ước lượng |
|---|---|---|
| `drills` | 8 diễn tập ngắn: drift/quét toàn bộ, audit DIVERGED, dead-letter, partition, **`rs-horizon` (chạy lại)**, MinIO chết, DB cửa hàng chậm **đủ 16 phút (`rs-db-pool`)**, **DB trung tâm chậm** (`rs-ingest-p95`, `rs-store-lag` có chủ đích) | ≤ 2,5 giờ |
| `drill-store_offline_70min` | worker store-003 chết ~70 phút | ~1,5 giờ |
| `drill-transform_stalls` | **MỚI — `rs-transform`**: DAG tạm dừng, S4/S5 vẫn chạy từ ngoài (`pipeline run`) → đơn đã nạp mà chưa vào mart quá 70 phút | ≤ 2,5 giờ |
| `drill-pipeline_and_maintenance_stop` | **MỚI — `rs-loaded-until` + `rs-maintenance`**: DAG tạm dừng VÀ `central-maintenance` chết > 3 giờ | ~3,2 giờ |
| `load` | LD-1 + overhead OTel, LD-2 (10 → 200 cửa hàng ảo), LD-4 (**`rs-central-pg-conn`**) | ~1,5 giờ |
| `restore` | **MỚI — khôi phục thật** `store_backup.py restore` (§3) | ~15 phút |
| `chaos` (không nằm trong `all`) | CH-1…CH-7 trên máy sạch (đã đạt trên máy dev) | ~1,5 giờ |

**Test bị BỎ QUA làm job ĐỎ.** Test opt-in tự skip khi thiếu cờ, và diễn tập cảnh báo tự skip khi
Grafana chưa trả lời. pytest coi skip là xanh, nên một nhóm có thể "đạt" trong vài giây mà không chứng
minh gì. Vì vậy job đọc `junit.xml` và đỏ nếu có test bị skip, còn `bootstrap.py --observability` giờ
chờ Grafana lên rồi mới xong.

Nguyên tắc giữ nguyên: **không hạ ngưỡng, không sửa rule** để nó kêu. Các ngưỡng tính bằng giờ nên các
diễn tập đó dài, và mỗi cái chiếm một máy.

**Kết quả** là artifact `proof-<nhóm>` (giữ 30 ngày): gồm `runs/alerts|load|chaos/<phiên>/*.json`, nhật
ký cảnh báo và bảng tổng kết (`report.txt`), bản ghi của `alert-sink`, `machine.txt`, `docker stats`, và
log compose khi đỏ. Khóa cửa hàng (`*.env`) không vào artifact.

**Hai điều phải nhớ khi đọc số:**
- Số đo tải (LD-1/2/4, overhead OTel) là của **runner 4 vCPU dùng chung**, không phải laptop. Luôn trích
  kèm `machine.txt`. Tiêu chí "bộ giả lập < 50% CPU" có thể đỏ trên máy 4 nhân khi cả stack chạy cùng
  máy. Nếu đỏ, đó là giới hạn của máy đo, không phải kết luận về hệ thống.
- LD-2/LD-4 cần 200 cửa hàng **có thật trong master data**. Bộ fixture chỉ có ~32, nên nhóm `load` sinh
  bộ tổng hợp lớn hơn: `infra/loadtest/synth_master.py` = fixture + 260 cửa hàng tỉnh mẫu C (tổng 292).
  Không có nó, bậc "200" âm thầm chỉ đo ~29 cửa hàng.

## 3. Test khôi phục thật — `tests/scenarios/test_restore.py`

Trước đây chỉ có `verify` (khôi phục thử vào database tạm). Test mới thay database THẬT của store-001:

1. Bán B khi sync worker tắt → backup ngay (khởi động lại sidecar) → lúc backup, sự kiện của B còn chưa gửi.
2. Bật worker, B lên trung tâm. `verify` bản backup.
3. Bán C sau backup, đồng bộ xong.
4. `restore`. Kiểm: C **mất ở cửa hàng** nhưng **còn đủ ở trung tâm** (RPO); sự kiện B được gửi LẠI
   từ outbox đã khôi phục nhưng đối soát vẫn `CONVERGED` (idempotency, AT-03); **bán tiếp được ngay**
   (E) và E lên trung tâm bình thường.

Opt-in bằng `RETAIL_SYNC_CHAOS=1` (thay database thật).

## 4. Lượt chạy đầu (2026-09-28, commit `8f9c559`) — 2 nhóm xanh, 4 nhóm đỏ

Artifact đọc từ `runs/proof-ci/` (chủ dự án tải tay). Runner: 4 vCPU, 16 GB.

**Xanh:** `restore` (khôi phục thật đạt cả 4 điều kiện ở §3), `drill-pipeline_and_maintenance_stop`
(**`rs-loaded-until`, `rs-maintenance` kêu lần đầu**). Trong nhóm `load`, `rs-central-pg-conn` (LD-4)
và `rs-ingest-p95` (LD-2) cũng kêu và tới `alert-sink`.

**Đỏ, phân loại theo nguyên nhân:**

| Test | Nguyên nhân | Loại | Sửa |
|---|---|---|---|
| `store_offline_70min` (`rs-store-silent` không hề `pending` sau 100 phút) | Heartbeat của trung tâm là `UPDATE` → cửa hàng **chưa từng gửi sự kiện** không có dòng `store_sync_status` → vô hình với cảnh báo. Trên laptop store-003 có lịch sử nên không lộ | **Lỗi hệ thống** | Heartbeat thành UPSERT + test tích hợp `test_heartbeat_of_a_store_that_never_sold_makes_it_visible` |
| `transform_stalls` (80 phút không thấy đơn chờ dbt) | Bộ giám sát: fact rỗng → mốc 1970-01-01, `toDate(1970-01-01) - 7` **tràn `Date`** thành năm 2149 → lọc mất mọi đơn → báo 0 | **Lỗi hệ thống** (cùng họ lỗi tràn ngày ở dbt) | Mốc dưới tính ở Python, kẹp ở 1970-01-01 + test tích hợp `test_first_sales_waiting_for_dbt_are_counted_while_the_fact_is_still_empty`. ⚠️ Bản sửa đầu (`toDate32`) **sai**: so với cột `Date`, ClickHouse ép hằng về `Date` nên vẫn tràn — test mới bắt được trên CI (2026-10-07) |
| `drift` | Stack sạch chưa có khách nào có điểm | Lỗi test | Test tự bán 4 đơn cho khách mới qua Edge API trước |
| `slow_store` (`rs-db-pool`, `rs-sales-p95` không kêu) | Runner nhanh hơn laptop: DB ở 5% CPU vẫn theo kịp 4 đơn/s (p95 client 544 ms, rule lượn `pending` ↔ `inactive`) | Sự cố gây ra chưa đủ nặng | 2% CPU, 8 đơn/s, bóp SAU khi mở ca. Ngưỡng giữ nguyên |
| `slow_central` (`rs-ingest-p95` không kêu; `rs-store-lag` kêu) | 5 cửa hàng ảo không giữ đủ lô đồng thời | Sự cố chưa đủ nặng | 20 cửa hàng, 5% CPU |
| `otel_overhead` | p95 chốt đơn **11 → 28 ms (+146%)**, p50 +30%, CPU edge-api 8 → 13%, lặp lại y hệt ở 2 lần đo. Test chặn ở "< 50%" coi đó là phép đo hỏng | **Số đo thật**, test sai giả định | Đo thêm chế độ trace 10% (`infra/chaos/otel-sampled.compose.yaml`); test chỉ chặn khi phép đo không lặp lại được (lệch > 30%). Ghi `adr009_within_5pct` |
| `ld2` | p95 server `POST /events` > 1 s từ bậc 50 cửa hàng ảo | **Số đo thật** — xem dưới | Thêm `events_per_second` và CPU central-api/central-db mỗi bậc |

**LD-2 là phát hiện cần chủ dự án quyết.** Test nén một ngày 15 giờ vào 5 phút (×180; docs/16 §4 bản
nháp ghi ×10). Quy ra tải thật:

| Bậc (cửa hàng ảo) | Sự kiện/giây | p95 server | Bị từ chối (429/503) | Đúng dữ liệu |
|---|---|---|---|---|
| 10 | ~36 | 0,09 s | 2 | CONVERGED |
| 50 | ~164 | **1,6 s** | 1.831 | CONVERGED |
| 100 | ~197 | **2,8 s** | 6.478 | CONVERGED |
| 200 | ~291 | **2,2 s** | 19.826 | CONVERGED |

Tải thiết kế ở docs/02 §1 là ~260 lượt ghi/giây (T3, 2000 cửa hàng, đỉnh lễ Tết). Như vậy trên runner
này, **dữ liệu luôn đúng** (0 mất, 0 dead-letter, mọi bậc CONVERGED, mọi từ chối đều có `Retry-After`),
nhưng **độ trễ vượt 1 s ở dưới tải thiết kế**. Nghi phạm số 1: `central-api` chạy MỘT tiến trình
uvicorn (= một nhân), lại đang trace 100% (overhead ở trên). Runner cũng chia 4 nhân cho cả stack ~30
container và bộ giả lập. Lượt sau sẽ có CPU từng bậc để phân biệt "nghẽn một nhân" với "nghẽn DB".
Tăng số worker không phải sửa nhỏ: rate limit theo cửa hàng đang nằm trong bộ nhớ một tiến trình.

**CI 2026-10-07 (commit `5130782`) đỏ ở `integration`**, hai test, chạy lại trên máy để chẩn đoán:
- Test monitor mới: bản sửa `toDate32` sai (xem bảng trên) — sửa lại, test xanh.
- `test_virtual_stores_with_all_gate_a_quirks_converge_at_central`: **bom hẹn giờ trong test**, không
  liên quan heartbeat. `START = date(2026, 8, 31)` cố định; từ 1/10 tháng 8 không còn là "tháng trước"
  nên không còn partition `point_ledger` → điểm của đơn 31/8 bị từ chối vĩnh viễn → dead-letter. Giờ
  `START` = ngày cuối tháng trước, tính theo hôm nay.

## 4b. Lượt chạy thứ hai (2026-10-07, commit `aa42cec`) — 4/6 nhóm xanh

**Xanh:** `restore`, `drill-pipeline_and_maintenance_stop`, **`drill-store_offline_70min`** và
**`drill-transform_stalls`**. Hai bản sửa lỗi hệ thống ở §4 (heartbeat UPSERT, mốc ngày của bộ giám
sát) được chứng minh trên máy sạch, và **`rs-transform` kêu lần đầu**. Trong nhóm `drills`: drift,
`rs-horizon`, DB trung tâm chậm (`rs-ingest-p95` + `rs-store-lag` có chủ đích) đều đạt.

**Đỏ:**

| Test | Số đo | Nguyên nhân | Sửa (2026-10-08) |
|---|---|---|---|
| `slow_store` (`rs-db-pool`, `rs-sales-p95` không kêu trong 30 phút bóp) | 2% CPU: request chờ 40–70 s, chỉ 497/7.680 đơn xong trong 78 phút. Hai rule chỉ `pending` thoáng qua SAU khi gỡ bóp | Bóp CPU phụ thuộc tốc độ máy: 5% thì runner theo kịp, 2% thì Postgres không mở nổi kết nối overflow → pool kẹt ~10/20 | Thay bằng **khóa bảng `outbox`** (hiệu ứng như nhau trên mọi máy). Tách hai diễn tập: `store_pool_exhausted` (khóa liên tục → pool đầy) và `slow_store_database` (khóa từng nhịp 2 s / nhả 1 s → p95 ~2 s, pool không đầy) |
| `otel_overhead` | `sampled` lần 1: p50 13,4 ms, lần 2: 8,8 ms → lệch 106% | Container vừa dựng lại chưa "ấm", `sleep(10)` không đủ | Mỗi lần đo có một lượt bán khởi động 45 s, không tính số |
| `ld2` | p95 1,08 s ở 127 sự kiện/giây | **central-api chạm 100% MỘT nhân**, central-db chỉ 13–26% (bảng ở [ADR-011](../adr/011-central-api-multi-process.md)) | Chủ dự án chọn **nhiều tiến trình** → ADR-011: `--workers 4`, semaphore + pool chia theo tiến trình, metric có `service.instance.id` |

Số đo overhead OTel (đã ấm, lần đo tốt): trace 100% → p95 +116%, CPU edge-api 7,9 → 12,7%. Trace 10% →
p95 ~+27%, CPU ~10,3%. Cả hai đều vượt mức 5% của ADR-009. Phần còn lại ở 10% là metric +
instrumentation, không tắt được (observability là Must). Vẫn còn chờ quyết định tỉ lệ lấy mẫu.

## 5. Trạng thái (2026-10-08)

**Code:** commit sau `aa42cec` (cục bộ, chờ push): diễn tập khóa bảng, khởi động ấm cho đo OTel, và
**ADR-011 Central API nhiều tiến trình**. Đã chạy: 286 unit test, lint/mypy/import-linter sạch. **Chưa
chạy trên Docker:** compose `central-api --workers 4` sẽ được job `scenarios` của CI kiểm khi push.

**Cổng B** ([06](../06-roadmap.md)):

| Điều kiện | Trạng thái |
|---|---|
| CH-1…CH-7 | ✅ (2026-09-25, máy dev) |
| Cảnh báo đã kích hoạt thử | 🟡 **20/21** — còn `rs-db-pool` (diễn tập khóa bảng mới, chờ chạy) |
| Partition tháng sau tự tồn tại | ✅ |
| Backup + `restore` thật | ✅ (workflow `proof`, 2 lượt) |
| LD-1 | ✅ p95 chốt đơn 22 ms, bộ giả lập 1% CPU |
| LD-4 / 200 kết nối | ✅ chỉ `200`/`503` có `Retry-After`, 0 lỗi |
| **LD-2** | 🔴 p95 1,08 s ở 127 sự kiện/giây, nghẽn một nhân → ADR-011, **chờ đo lại** |
| **Overhead OTel** | 🔴 trace 100% +116%, trace 10% ~+27% — **chờ quyết định tỉ lệ lấy mẫu** |
| LD-3, seam 50 triệu dòng ledger | ⏳ cần máy chạy liên tục |
| DI-1…DI-5 | ⏳ DI-1…3 có trong job `scenarios`; DI-4/5 chưa rà |
| Test ngâm 72h | ⏳ chạy cuối cùng |
| Số đo ghi vào docs/02, `git clone` → README | ⏳ |

## 6. Các bước tiếp theo (theo thứ tự)

**Bước 1 — push, chờ CI thường xanh** (~15 phút). Push `main`. Workflow `CI` tự chạy: `quality`,
`unit`, `integration`, `scenarios`. Nếu đỏ: dán khoảng 80 dòng cuối của bước đỏ (từ `FAILURES` / `short
test summary info`). Hoặc cài `gh` (`winget install GitHub.cli` → `gh auth login`) để Claude tự đọc log.

**Bước 2 — chạy lại `proof`, chỉ hai nhóm đỏ** (~2,5 giờ). Actions → **proof (giai đoạn B)** → Run
workflow → `main`, suite **`drills`**, rồi chạy lần nữa với suite **`load`** (hai lượt song song được).
Chỉ cần chạy lại riêng một test thì điền `-k` (ví dụ `pool_exhausted`). Xong thì tải artifact về
`runs/proof-ci/<tên-artifact>/` (xóa lượt cũ trước) và báo Claude đọc. Kỳ vọng:
- `drills` xanh → **21/21 cảnh báo** đã kích hoạt thử.
- `load`: overhead OTel đo lặp lại được (test xanh). LD-2 với 4 tiến trình: xem CPU central-api có còn
  chạm trần không. Còn > 1 s mà CPU central-api KHÔNG chạm trần → nút thắt đã dời (DB, lock), đo tiếp.
- Artifact giữ 30 ngày.

**Bước 3 — chủ dự án quyết tỉ lệ lấy mẫu trace** (ADR-009: overhead > 5% → giảm). Số hiện có: 100% →
+116% p95, 10% → ~+27% (phần còn lại là metric, không tắt được). Đặt bằng
`OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG` trong compose và ghi
changelog ADR-009. Mức 5% của ADR-009 có thể phải sửa thành con số đo được, vì metric luôn tốn phí.

**Bước 4 — máy chạy liên tục** (VPS ARM ~16 GB, hoặc máy dev lúc rảnh), cho những gì runner không làm
được (> 6 giờ hoặc > 14 GB đĩa):
- **Seam 50 triệu dòng ledger**: `make seam-ledger WORK="runs/bulk-t2-24m-s42/work runs/bulk-t2-extra-s43/work"`
  (dữ liệu đã sinh trên máy dev; VPS thì sinh lại bằng `make sim-bulk`).
- **LD-3**: bản dbt cũ (commit `9d13cb0`) vài tháng đầu để có số "trước", rồi bản hiện tại đủ 24 tháng +
  truy vấn lớp C: `make ld3`.
- Ghi số đo vào docs/02, rà DI-4/DI-5, chạy thử `git clone` → README trên máy sạch.

**Bước 5 — test ngâm 72h** (cuối cùng, sau khi mọi thứ trên xanh): `edge` t0 + `virtual` t1 chạy nền
3 ngày, `audit --watch` + nhật ký cảnh báo. Đạt: không rò bộ nhớ, `outbox` không bloat, p95 không trôi,
`audit` không lần nào `DIVERGED`. Đây là việc duy nhất BẮT BUỘC cần máy chạy liên tục 3 ngày.

Xong cả 5 bước → đánh dấu cổng B trong docs/06 → giai đoạn C ([ADR-010](../adr/010-data-flow-first.md)).
