# 16 — Bản đồ kịch bản → test

> Các kịch bản AT/CH/LD/DI trong [01-requirements](01-requirements.md) và
> [08-reliability-and-scale](08-reliability-and-scale.md) hiện chỉ là mô tả. Doc này map
> sang cấu trúc file test thật, để không ai phải "dịch lại" khi bắt tay viết.

## 1. Cấu trúc thư mục test

```
tests/
├── unit/                    # logic thuần — KHÔNG cần Docker, chạy < 1s
│   ├── test_pricing.py          # ✅ tính tiền, làm tròn, cộng gộp chiết khấu (Q-B1, Q-B2)
│   ├── test_points.py           # ✅ tính điểm, xếp hạng theo lifetime_earned
│   ├── test_place_sale.py       # ✅ use case PlaceSale chạy bằng fake (không cần Docker)
│   ├── test_config_rules.py     # ✅ mặc định quy tắc nghiệp vụ khớp .env.example
│   ├── test_event_schema.py     # ✅ validate envelope + payload theo docs/12
│   ├── test_security.py         # ✅ Argon2 + JWT — roundtrip, hết hạn, sai chữ ký
│   ├── test_auth_domain.py      # ✅ xếp hạng vai trò cho require_role()
│   ├── test_login.py            # ✅ use case Login bằng fake
│   ├── test_seed_data.py        # ✅ parser crawl_data/ — mã trùng, barcode trùng
│   ├── test_tracing.py          # ✅ OTel: idempotent qua nhiều lần create_app(), inject/link W3C
│   ├── test_sync_client.py      # ✅ mọi lỗi cấp request → lỗi đường truyền; Retry-After; jitter
│   ├── test_pii.py              # ✅ chuẩn hóa SĐT VN (mọi cách viết → một hash), HMAC có khóa, lỗi không lộ số
│   ├── test_shifts.py           # ✅ mở/đóng ca bằng fake: business_date theo giờ cửa hàng, variance ghi nguyên trạng
│   ├── test_simulator_generator.py # ✅ bộ sinh: tái lập theo seed, phân bố khớp profile, không đơn nào lúc giao ca
│   ├── test_simulator_virtual.py   # ✅ tật, đồng hồ tổng hợp, file khóa, import THUẦN (không kéo DB/FastAPI),
│   │                               #    audit thử lại khi đứt kết nối ClickHouse
│   ├── test_console.py          # ✅ 6 CLI in được tiếng Việt khi stdout không phải UTF-8 (Windows cp1252)
│   ├── test_migrations.py       # ✅ ID revision ≤ 32 ký tự (`alembic_version.version_num`)
│   ├── test_ingest_classification.py  # ✅ SQLSTATE → retryable/dead-letter; không lộ input vào lý do lỗi
│   └── test_loyalty_defaults.py # ✅ quy tắc hạng/tích điểm mặc định edge == central (không import chéo được)
├── integration/              # testcontainers — Postgres/Redis/ClickHouse thật
│   ├── test_edge_schema.py      # ✅ bất biến DB cửa hàng: INV-1/2/3, trả hàng, ca, outbox
│   ├── test_central_schema.py   # ✅ partition point_ledger tự tạo, idempotency gate
│   ├── test_place_sale.py       # ✅ transaction đầy đủ: sale+line+payment+ledger+outbox
│   ├── test_auth_http.py        # ✅ FastAPI thật: login, require_role, chống giả mạo employee_id
│   ├── test_web_ui.py           # ✅ UI POS qua HTTP thật: đăng nhập, mở ca, quét, thanh toán
│   ├── test_seed.py             # ✅ central.ops.seed / edge.ops.seed trên Postgres thật
│   ├── test_sync_pipeline.py    # ✅ place_sale → outbox → worker → POST /events thật: AT-01/02/03/04,
│   │                            #    CH-7, C03, backpressure, dead-letter, chống giả cửa hàng
│   ├── test_customers_shifts.py # ✅ POST /customers (chỉ hash, trùng SĐT nhiều cách viết, 8 quầy đồng thời),
│   │                            #    /shifts/open|close (409 kèm shift_id, chỉ đếm TIỀN MẶT, race đóng ca ↔ chốt đơn)
│   ├── test_reconciliation.py   # ✅ DI-1/INV-4 tăng dần: lệch được lưu tới khi sửa, `--full` thấy cái
│   │                            #    tăng dần bỏ qua (đánh đổi có chủ đích của ràng buộc #8)
│   ├── test_extract_watermarks.py # ✅ bẫy 1 (commit muộn KHÔNG lọt, `extract_horizon()`, thiếu quyền → từ chối,
│   │                              #    `transaction_timeout` cắt thật), bẫy 2 (index mọi partition), bẫy 3 (trigger)
│   ├── test_clickhouse_bronze.py  # ✅ bẫy 4 trên ClickHouse thật: mọi bảng có cửa sổ khử trùng, chạy lại không
│   │                              #    nhân đôi, token phải có hash, cửa sổ có hạn, dựng lại bằng DROP PARTITION
│   ├── test_pipeline.py         # ✅ S4+S5 trên PG + MinIO + ClickHouse thật: mọi dòng tới bronze, chạy lại
│   │                            #    không nhân đôi, mất nhật ký nạp vẫn không nhân đôi, dựng lại từ lake,
│   │                            #    dòng commit muộn không lọt, UPDATE → phiên bản mới + file cũ bất biến,
│   │                            #    đổi độ dài cửa sổ không chồng lấn, bảng rỗng có file mốc, một lượt mỗi lúc (lock)
│   ├── test_dbt_marts.py        # ✅ dbt THẬT trên ClickHouse thật: khớp tới từng đồng, bẫy 5 (tháng bị ảnh hưởng),
│   │                            #    nạp dở dang, phiên bản mới nhất, bronze nhân đôi → đỏ, inferred member, ca đang mở
│   ├── test_simulator_virtual.py # ✅ cửa hàng ảo + 3 tật trên Central API thật → CONVERGED; đối chứng mất đơn
│   ├── test_simulator_edge.py   # ✅ bộ giả lập vào Edge API thật → worker → Central thật → pipeline → dbt → audit:
│   │                            #    L0 = L1 = L2 = L3 = L4; mất đơn ở trung tâm / bronze / mart → DIVERGED
│   ├── test_bulk_schema.py      # ✅ B: schema Parquet của bulk == schema bước trích xuất ghi ra
│   └── test_partition_clock.py  # ✅ B: libfaketime — 14 lần sang tháng, partition tự tồn tại
└── scenarios/                # end-to-end trên compose thật, NGUỒN DỮ LIỆU = BỘ GIẢ LẬP
    ├── conftest.py               # ✅ OPT-IN (RETAIL_SYNC_SCENARIOS=1, `make test-scenarios`): đổi trạng thái compose
    ├── test_at.py                # ✅ AT-01..04 (A) · AT-06 (A-Should) · AT-05, 09 (C)
    ├── harness.py                 # ✅ công cụ chung: gây sự cố bằng docker, lịch viết tay, Grafana
    ├── test_chaos.py              # ✅ CH-1 .. CH-7 (B) — OPT-IN RETAIL_SYNC_CHAOS=1, cả 7 đạt 2026-09-25
    ├── test_alert_drills.py       # ✅ diễn tập từng cảnh báo bằng điều kiện thật (B) — RETAIL_SYNC_ALERT_DRILLS=1
    ├── test_load.py               # 🟡 LD-1, LD-2, LD-4, overhead OTel (B) — RETAIL_SYNC_LOAD=1; LD-3 đọc báo cáo infra/loadtest/
    ├── test_soak.py               # ⏳ test ngâm 72h (B) — `audit` định kỳ, không bao giờ DIVERGED
    └── test_data_integrity.py     # ✅ AT-07, AT-10, DI-1..3 · DI-4/5 = một lần `simulator audit` (17 §5)
```

**Điều kiện đạt chung của mọi test trong `scenarios/`:** sau kịch bản, `simulator audit` trả
`CONVERGED` trong thời hạn hội tụ của kịch bản đó ([18 §6](18-simulator.md)). Riêng từng kịch
bản thì cộng thêm tiêu chí của nó (p95, thời gian hội tụ...).

## 2. Kịch bản nghiệp vụ (AT) → test

| # | File · hàm | Cần hạ tầng | Giai đoạn |
|---|---|---|---|
| AT-01 | `test_at.py::test_normal_sale_with_known_customer` | edge stack + bộ giả lập `edge` | A |
| AT-02 | `test_at.py::test_offline_then_reconnect_ten_sales` | edge + central, `docker network disconnect` | A |
| AT-03 | `test_at.py::test_duplicate_sync_event_five_times` | edge + central | A |
| AT-04 | `test_at.py::test_concurrent_purchase_two_stores` | central + bộ giả lập `virtual` (2 cửa hàng ảo cùng bán cho một khách cùng lúc). Ở chế độ `edge`, cửa hàng 2 chưa biết được khách của cửa hàng 1 — tra khách qua trung tâm là AT-05, giai đoạn C | A |
| AT-05 | `test_at.py::test_new_store_pulls_existing_balance` | edge + central | C |
| AT-06 | `test_at.py::test_return_reverses_points` | edge + central | A — Should |
| AT-07 | `test_data_integrity.py::test_pipeline_idempotent_on_rerun` | toàn luồng + data platform: DAG hai lần liên tiếp, số và **tên part** của fact không đổi | A |
| AT-08 | `test_chaos.py::test_redis_down_sale_still_works` | edge, tắt Redis | B |
| AT-09 | `test_at.py::test_tier_upgrade_applies_next_sale` | edge + central | C |
| AT-10 | `test_data_integrity.py::test_ledger_matches_balance` | central: `central.ops.reconcile --full`, drift = 0 | A |

## 3. Kịch bản hỗn loạn (CH) → test

| # | File · hàm | Công cụ |
|---|---|---|
| CH-1 | `test_chaos.py::test_ch1_network_disconnect_30min` | `docker network disconnect`/`connect --alias central-api` |
| CH-2 | `test_chaos.py::test_ch2_kill_app_mid_commit_x20` | `docker kill -s SIGKILL`, lặp trong loop |
| CH-3 | `test_chaos.py::test_ch3_kill_postgres_edge` | `docker kill`, kiểm tra WAL recovery |
| CH-4 | `test_chaos.py::test_ch4_disk_95_percent` | volume tmpfs 512 MB của cửa hàng thử + `fallocate` (không đụng đĩa thật) |
| CH-5 | `test_chaos.py::test_ch5_redis_down` | `docker stop edge-cache` |
| CH-6 | `test_chaos.py::test_ch6_ten_stores_reconnect_simultaneously` | 10 cửa hàng ẢO (docs/18 §3) cùng offline rồi cùng nối lại — 10 stack thật không vừa RAM |
| CH-7 | `test_chaos.py::test_ch7_resend_batch_ten_times` (+ `..._virtual_stores_...`) | gọi lại `POST /events` cùng payload; tật `resend` với `resend_times=10` |

## 4. Kịch bản tải (LD) → bộ giả lập

Không dùng `k6` (đổi 2026-09-23, lý do ở [18 §9](18-simulator.md)). Mỗi LD là **một lệnh bộ
giả lập + một ngưỡng**. `test_load.py` chạy lệnh đó rồi đọc báo cáo JSON của bộ giả lập.

| # | Lệnh (rút gọn) | Đạt khi |
|---|---|---|
| LD-1 | `run --mode edge --stores 1 --rate x50` | p95 `POST /sales` < 500 ms, bộ giả lập < 50% CPU |
| LD-2 | `run --mode virtual` 10 → 50 → 100 → 200 cửa hàng, nhịp ×180 (một ngày 15 giờ trong 5 phút; ×10 của bản nháp chỉ ~16 sự kiện/giây — xa dưới tải thiết kế ~260/giây của [02 §1](02-scale-capacity.md)). Mỗi bậc ghi sự kiện/giây + CPU trung tâm | p95 `POST /events` < 1 s, 503 có `Retry-After`, `audit` hội tụ |
| LD-3 | `bulk --profile t2 --months 24` rồi bộ truy vấn lớp C | Mỗi truy vấn < 3 s |
| LD-4 | `run --mode virtual --stores 200 --connections 200` | Không lỗi, hoặc 503 tử tế |

Chạy LD-1 với và không có OpenTelemetry bật để đo overhead (bắt buộc theo [ADR-009](adr/009-observability-stack.md)):
```bash
OTEL_SDK_DISABLED=true  uv run python -m simulator run --mode edge --stores 1 --rate x50 --duration 15m
OTEL_SDK_DISABLED=false uv run python -m simulator run --mode edge --stores 1 --rate x50 --duration 15m
```

## 5. Toàn vẹn dữ liệu (DI) → test

| # | Bất biến kiểm tra | File |
|---|---|---|
| DI-1 | `point_balance.balance = SUM(point_ledger.delta)` | `test_data_integrity.py::test_ledger_balance_matches` |
| DI-2 | `sale.subtotal = SUM(sale_line.line_total) − discount` | `test_data_integrity.py::test_sale_line_total_matches` |
| DI-3 | `SUM(sale_payment.amount) = sale.total` | `test_data_integrity.py::test_payment_sum_matches` |
| DI-4 | Số dòng bronze = số dòng warehouse theo phân vùng | `test_data_integrity.py::test_warehouse_row_count_matches_bronze` |
| DI-5 | Mọi `sale` cửa hàng có mặt ở trung tâm sau ≤ 1h | `test_data_integrity.py::test_sale_replicated_within_sla` |

Cả năm DI là các ô của **bảng đối soát xuyên tầng** ([17 §5](17-data-flow.md)), nên một lần
`simulator audit` kiểm tất cả. Các hàm ở trên chỉ là cách gọi tên từng ô khi test đỏ.

## 6. Ngưỡng "xanh" cho CI

| Loại | Chạy khi nào | Bắt buộc pass để merge? |
|---|---|---|
| `unit/` | Mọi commit (pre-commit + CI) | ✅ Có |
| `integration/` | Mọi PR | ✅ Có |
| `simulator/` | Mọi commit | ✅ Có |
| `scenarios/test_at.py`, `test_data_integrity.py` | Mọi PR | ✅ Có (từ cổng A). **Job CI `scenarios`** (2026-09-25): `infra/bootstrap.py` dựng compose từ con số 0 trên dữ liệu tổng hợp `tests/fixtures/crawl_data/`, rồi `pytest tests/scenarios` + `pipeline health` |
| `scenarios/test_chaos.py`, `test_load.py`, `test_soak.py` | Trước khi coi cổng B đạt, và theo lịch hằng tuần sau đó | ⚠️ Không chặn PR nhỏ, nhưng bắt buộc cho cổng B |
| `scenarios/test_alert_drills.py`, `test_load.py`, `test_restore.py`, `test_chaos.py` trên máy sạch | Bấm tay: **workflow `proof`** (2026-09-28, `.github/workflows/proof.yml`) — mỗi nhóm một runner 4 vCPU/16 GB chạy song song, ≤ 6 giờ, kết quả là artifact `proof-<nhóm>` | ⚠️ Bằng chứng cổng B, không chặn PR. Số đo tải thuộc về runner, ghi kèm `machine.txt`. Không chạy được ở đây: test ngâm 72h, LD-3, seam 50 triệu dòng (quá 6 giờ / quá đĩa) |

> **Tiến độ AT (2026-09-23):** AT-01, AT-02, AT-03 và phần trung tâm của AT-04 đã có test ở
> mức tích hợp (`tests/integration/test_sync_pipeline.py`) — Postgres thật + Central API thật
> qua ASGI, chưa phải compose. AT-02 đã chạy thêm một lần tay trên compose thật (tắt
> `central-api`, bán 3 đơn, bật lại → lên đủ, `attempts` = 0). ✅ **2026-09-24:** `scenarios/test_at.py`
> + `test_data_integrity.py` chạy trên compose thật bằng bộ giả lập — xem
> [progress/2026-09-24-cong-a.md](progress/2026-09-24-cong-a.md).

**Chạy `tests/scenarios/` trên một stack khác stack dev** (CI, hay một project compose riêng để không
đụng dữ liệu đang có): cùng bộ test, đổi bằng biến môi trường —
`RETAIL_SYNC_COMPOSE_PROJECT` (tiền tố tên container), `RETAIL_SYNC_ENV_FILE` (secret + khóa),
`RETAIL_SYNC_VIRTUAL_KEYS`, `RETAIL_SYNC_CRAWL_DATA`. Ví dụ dựng stack thử song song với nguồn
riêng: `uv run python infra/bootstrap.py --project retail-sync-ci --env-file runs/ci.env
--virtual-keys runs/ci-virtual-keys.env --crawl-data tests/fixtures/crawl_data` (cổng host trùng
stack dev — dừng stack dev trước).

---
*Changelog: 2026-09-28 (lần 2) — §4 LD-2 theo nhịp thật của test (×180, không phải ×10) và quy ra sự kiện/giây.*

*Changelog: 2026-09-28 — §6: workflow `proof` (diễn tập cảnh báo, tải, khôi phục, hỗn loạn trên runner dùng một lần).*

*Changelog: 2026-09-25 (lần 2) — §6: job CI `scenarios` + `infra/bootstrap.py` + biến chọn stack.*

*Changelog: 2026-09-25 — §1 theo file thật (`test_dbt_marts.py`, `test_simulator_virtual.py` ×2,
`test_console.py`, `test_simulator_edge.py` tới L4); `scenarios/` đánh dấu ✅/⏳; §2 AT-04 bằng
chế độ `virtual`; §6 kịch bản compose hiện chạy tay.*

*Changelog: 2026-09-23 (lần 3) — thêm `test_extract_watermarks.py`, `test_clickhouse_bronze.py`
(bẫy 1–4). Fixture `clickhouse` ở `tests/integration/conftest.py` dùng cùng image với compose.*

*Changelog: 2026-09-23 (lần 2) — theo [ADR-010](adr/010-data-flow-first.md): cột giai đoạn cho AT;
`scenarios/` lấy dữ liệu từ bộ giả lập, đạt = `audit` `CONVERGED`; thêm test cho S4/S5 và 5 bẫy
ở docs/17 §4; LD chạy bằng bộ giả lập thay `k6`; thêm `test_soak.py`.*

*Changelog: 2026-09-23 — thêm `test_sync_pipeline.py` (thay tên `test_sync_worker.py` dự kiến:
nó kiểm cả hai đầu của đường đồng bộ, không riêng worker) và 3 file unit.*

*Changelog: 2026-09-17 — đánh dấu các file test đã viết; gộp `test_partition_autocreate.py`
vào `test_central_schema.py` (cùng cơ chế, tách file chỉ làm hai chỗ cùng dựng container).*

*Changelog: 2026-09-11 — tạo mới.*
