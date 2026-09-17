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
│   └── test_tracing.py          # ✅ OTel: idempotent qua nhiều lần create_app(), inject/link W3C
├── integration/              # testcontainers — Postgres/Redis/ClickHouse thật
│   ├── test_edge_schema.py      # ✅ bất biến DB cửa hàng: INV-1/2/3, trả hàng, ca, outbox
│   ├── test_central_schema.py   # ✅ partition point_ledger tự tạo, idempotency gate
│   ├── test_place_sale.py       # ✅ transaction đầy đủ: sale+line+payment+ledger+outbox
│   ├── test_auth_http.py        # ✅ FastAPI thật: login, require_role, chống giả mạo employee_id
│   ├── test_web_ui.py           # ✅ UI POS qua HTTP thật: đăng nhập, mở ca, quét, thanh toán
│   ├── test_seed.py             # ✅ central.ops.seed / edge.ops.seed trên Postgres thật
│   ├── test_sync_worker.py      # outbox → central, idempotency
│   └── test_reconciliation.py   # DI-1: balance = SUM(ledger.delta)
└── scenarios/                # kịch bản end-to-end, nhiều service thật (compose)
    ├── test_at.py                # AT-01 .. AT-10
    ├── test_chaos.py              # CH-1 .. CH-7
    ├── test_load.py               # LD-1 .. LD-4 (k6, chạy riêng, không pytest)
    └── test_data_integrity.py     # DI-1 .. DI-5
```

## 2. Kịch bản nghiệp vụ (AT) → test

| # | File · hàm | Cần hạ tầng |
|---|---|---|
| AT-01 | `test_at.py::test_normal_sale_with_known_customer` | edge stack |
| AT-02 | `test_at.py::test_offline_then_reconnect_ten_sales` | edge + central, `docker network disconnect` |
| AT-03 | `test_at.py::test_duplicate_sync_event_five_times` | edge + central |
| AT-04 | `test_at.py::test_concurrent_purchase_two_stores` | 2× edge + central |
| AT-05 | `test_at.py::test_new_store_pulls_existing_balance` | edge + central |
| AT-06 | `test_at.py::test_return_reverses_points` | edge |
| AT-07 | `test_data_integrity.py::test_pipeline_idempotent_on_rerun` | data platform |
| AT-08 | `test_chaos.py::test_redis_down_sale_still_works` | edge, tắt Redis |
| AT-09 | `test_at.py::test_tier_upgrade_applies_next_sale` | edge + central |
| AT-10 | `test_data_integrity.py::test_ledger_matches_balance` | central |

## 3. Kịch bản hỗn loạn (CH) → test

| # | File · hàm | Công cụ |
|---|---|---|
| CH-1 | `test_chaos.py::test_network_disconnect_30min` | `docker network disconnect`/`connect` |
| CH-2 | `test_chaos.py::test_kill_app_mid_commit_x20` | `docker kill -s SIGKILL`, lặp trong loop |
| CH-3 | `test_chaos.py::test_kill_postgres_edge` | `docker kill`, kiểm tra WAL recovery |
| CH-4 | `test_chaos.py::test_disk_95_percent` | `fallocate` file giả lập, hoặc volume nhỏ |
| CH-5 | `test_chaos.py::test_redis_down` | `docker stop edge-cache` |
| CH-6 | `test_chaos.py::test_ten_stores_reconnect_simultaneously` | 10× edge container, đồng loạt `network connect` |
| CH-7 | `test_chaos.py::test_resend_batch_ten_times` | gọi lại `POST /events` cùng payload |

## 4. Kịch bản tải (LD) → k6 script

Không dùng pytest — dùng `k6` (đã chọn ở [08-reliability-and-scale §6.3](08-reliability-and-scale.md)).

```
tests/load/
├── ld1_single_store_peak.js
├── ld2_ten_stores_sync.js
├── ld3_warehouse_query_t2.js
└── ld4_two_hundred_connections.js
```

Chạy với và không có OpenTelemetry bật để đo overhead (bắt buộc theo [ADR-009](adr/009-observability-stack.md)):
```bash
k6 run tests/load/ld1_single_store_peak.js --env OTEL=off
k6 run tests/load/ld1_single_store_peak.js --env OTEL=on
```

## 5. Toàn vẹn dữ liệu (DI) → test

| # | Bất biến kiểm tra | File |
|---|---|---|
| DI-1 | `point_balance.balance = SUM(point_ledger.delta)` | `test_data_integrity.py::test_ledger_balance_matches` |
| DI-2 | `sale.subtotal = SUM(sale_line.line_total) − discount` | `test_data_integrity.py::test_sale_line_total_matches` |
| DI-3 | `SUM(sale_payment.amount) = sale.total` | `test_data_integrity.py::test_payment_sum_matches` |
| DI-4 | Số dòng bronze = số dòng warehouse theo phân vùng | `test_data_integrity.py::test_warehouse_row_count_matches_bronze` |
| DI-5 | Mọi `sale` cửa hàng có mặt ở trung tâm sau ≤ 1h | `test_data_integrity.py::test_sale_replicated_within_sla` |

## 6. Ngưỡng "xanh" cho CI

| Loại | Chạy khi nào | Bắt buộc pass để merge? |
|---|---|---|
| `unit/` | Mọi commit (pre-commit + CI) | ✅ Có |
| `integration/` | Mọi PR | ✅ Có |
| `scenarios/test_at.py` | Mọi PR | ✅ Có |
| `scenarios/test_chaos.py`, `test_load` (k6) | Trước khi merge vào `main` từ nhánh tuần 4, và theo lịch hằng tuần sau đó | ⚠️ Không chặn PR nhỏ, nhưng bắt buộc trước khi coi "cổng tuần 4" đạt |

---
*Changelog: 2026-09-17 — đánh dấu các file test đã viết; gộp `test_partition_autocreate.py`
vào `test_central_schema.py` (cùng cơ chế, tách file chỉ làm hai chỗ cùng dựng container).*

*Changelog: 2026-09-11 — tạo mới.*
