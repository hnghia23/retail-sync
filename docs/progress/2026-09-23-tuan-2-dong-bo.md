# Tiến độ tuần 2 — đồng bộ cửa hàng → trung tâm (2026-09-23)

> Nhật ký tiến độ, không phải docs thiết kế. Ghi lại **đã làm gì, kiểm chứng ra sao, còn gì
> dang dở**. Nối tiếp [2026-09-18-tong-ket-tuan-1.md](2026-09-18-tong-ket-tuan-1.md) §6 —
> xong việc #1 (sync worker) và #2 (`POST /events`).

## 1. Đã làm

### Trung tâm — `POST /api/v1/events`
- `central/ingest/service.py` — một transaction cho lô, **một SAVEPOINT mỗi sự kiện**: sự kiện
  lỗi không kéo cả lô. Kể cả `processed_event` cũng nằm trong savepoint (áp không thành thì
  không bị đánh dấu "đã xử lý").
- `central/ingest/handlers.py` — handler cho `SaleCompleted` (3 bảng replica từ 1 sự kiện),
  `Points*` (ledger + snapshot `point_balance` cộng dồn bằng upsert + hạng suy từ `tier_rule`
  của trung tâm), `CustomerCreated/Updated` (kèm dò trùng C03), `ShiftClosed`.
- Phân loại lỗi `retryable` true/false theo SQLSTATE (bảng ở docs/13 §2) — sự kiện hỏng cấu
  trúc vào `dead_letter_event` ngay, sự kiện chờ phụ thuộc thì chờ.
- Xác thực cửa hàng: `central/auth.py` (khóa SHA-256) + `central/ops/provision_store.py` (cấp
  khóa, đăng ký cửa hàng). `store_id` trong từng envelope phải khớp chủ khóa.
- Backpressure: giới hạn 500 sự kiện/lô (`413`), 8 request đồng thời (`503 + Retry-After`).
- Seed trung tâm giờ nạp cả 4 quy tắc hạng + quy tắc tích điểm mặc định.

### Cửa hàng — sync worker (`edge/sync/`)
- `worker.py::run_once` / `run_forever` — `FOR UPDATE SKIP LOCKED`, xả tồn đọng liên tục khi
  lô đầy, không bao giờ tự thoát vì lỗi.
- **Tách lỗi đường truyền khỏi lỗi sự kiện**: mất mạng/401/503 → lùi cả vòng lặp, KHÔNG tăng
  `attempts`. Trung tâm từ chối → backoff theo `next_attempt_at` của riêng sự kiện đó.
- `client.py` — mọi thất bại cấp request thành `CentralUnavailableError`; `200` không đọc được
  KHÔNG bị coi là thành công.
- Thiếu `CENTRAL_API_KEY` → worker thoát ngay với lệnh cấp khóa, thay vì nhận 401 mãi.

### Migration
- `0003_central_ingest`: bỏ UNIQUE `customer.phone_hash` (C03), thêm `store_credential`.
- `0003_edge_sync`: `outbox.next_attempt_at` + trigger `outbox.sent_at → point_ledger_local.synced_at`.

## 2. Lỗi thật tìm thấy và đã sửa

| # | Lỗi | Hậu quả nếu để nguyên |
|---|---|---|
| 1 | `CLAIM_BATCH` (viết ở tuần 1) lọc thiếu `dead_lettered_at IS NULL` | Sự kiện đã dead-letter bị lấy lại mãi; query không khớp partial index |
| 2 | Không gì set `point_ledger_local.synced_at` | Sau khi đồng bộ, số dư ở quầy cộng điểm **hai lần** |
| 3 | `customer.phone_hash UNIQUE` ở trung tâm | Case C03 làm điểm của khách thứ hai **mất khỏi trung tâm** (vỡ khóa ngoại vĩnh viễn) |
| 4 | Sơ đồ docs/14 §4 tăng `attempts` khi mất mạng | Cửa hàng offline vài phút tự vứt dữ liệu đúng vào dead-letter. Đã đính chính docs |
| 5 | Không có backoff theo từng sự kiện | Sự kiện chờ khách đốt hết 10 lượt thử trong ~20 giây |
| 6 | Không xác thực ở trung tâm | Cửa hàng A ghi được sự kiện mang danh cửa hàng B |

Và một lỗi **của test** (không phải của code): `test_points_wait_for_missing_customer_then_land`
đỏ ~1–3% số lần vì jitter toàn phần có thể ra vài mili-giây. Tái hiện chắc chắn bằng cách ép
backoff = 0, rồi sửa test cho tất định (cố định backoff), chạy 5/5 lần xanh.

## 3. Kiểm chứng

**Tự động** — 265/265 test (thêm 32 unit + 13 tích hợp). Lint, `mypy --strict`, 8/8 hợp đồng
`import-linter` sạch.

**Kiểm đột biến** — đưa từng lỗi quan trọng trở lại, xác nhận test đỏ (5/5 bị bắt): mất mạng
tính vào `attempts`; bỏ lọc `dead_lettered_at`; bỏ backoff theo sự kiện; bỏ kiểm `store_mismatch`;
trigger `synced_at` không làm gì.

**Trên compose thật** (`edge` + `central`, image build lại):
1. Migration `0003` chạy trên DB thật đang có dữ liệu tuần 1.
2. Seed trung tâm (3515 cửa hàng, 5556 sản phẩm, 4 quy tắc hạng) + cấp khóa `store-001`.
3. Worker khởi động → **4 `SaleCompleted` tồn đọng từ 17/09 lên trung tâm ngay lượt đầu**.
   `store_sync_status` = `LAGGING` (~6 ngày) — trung thực, sự kiện mới nhất là từ 17/09.
4. **AT-02 bằng tay**: `docker stop central-api` → bán 3 đơn qua API (đều `201`) → worker lùi
   0.5s/4.0s/2.7s, `attempts` giữ nguyên 0 → `docker start` → 3 đơn lên đủ trong một lượt,
   trạng thái tự chuyển `OK`. Hai đầu khớp: 7 đơn, 1.200.000đ.

## 4. Còn dang dở — xếp lại theo hướng mới (ADR-010)

> 🔀 **Cùng ngày, sau khi xong đồng bộ, chủ dự án đổi hướng** ([ADR-010](../adr/010-data-flow-first.md)):
> chỉ làm **luồng dữ liệu** và **bằng chứng ổn định**. Dữ liệu vào bằng bộ giả lập, không qua UI.
> Tính năng để sau cổng B. Danh sách cũ ở mục này được xếp lại như dưới. Lộ trình đầy đủ ở
> [06-roadmap](../06-roadmap.md).

**Giai đoạn A — theo thứ tự nên làm**
1. ✅ ~~**`RegisterCustomer` + `POST /customers` (không UI) và `OpenShift` + `POST /shifts/open`**,
   kèm seed nhân viên.~~ Xong, kèm `CloseShift` (xem §7). Hiện không có chỗ nào `INSERT` vào `customer_local`, nên điểm chưa bao
   giờ đi qua đường đồng bộ thật (chỉ trong test tích hợp). `CustomerCreated` phải vào outbox
   TRƯỚC `PointsEarned` của khách đó (outbox theo `id`).
2. ✅ ~~**Bộ giả lập** bước 1–3~~ — xong, xem §8.
3. ✅ ~~**Đối soát INV-4 tăng dần**~~ — xong, xem §8.
4. ~~Sửa trước bẫy 1–3~~ → ✅ **xong cùng ngày, kèm bẫy 4**, xem §6.
5. Trích xuất → bronze → ClickHouse → dbt → Airflow. Còn lại của spike S4: `s3()` từ MinIO.
6. *Should:* đóng ca + `ShiftClosed`; trả hàng. **`SaleReturned` phải mở rộng payload** trước
   khi trung tâm có handler: docs/12 §3.2 thiếu `business_date`, `employee_id`, `shift_id`,
   tổng tiền, đều là cột bắt buộc của `sale_replica`.

**Dời sang giai đoạn C**
- Tra khách 3 bước (Redis → Postgres cửa hàng → `GET /customers/*` trung tâm, 500ms). Lưu ý khi
  làm: sự kiện đồng bộ SAU lần kéo số dư sẽ không nằm trong `cached_balance` lẫn phần "chưa đồng
  bộ", nên số dư hiển thị tụt tạm cho tới lần kéo sau. Cần làm mới cache khi đồng bộ.
- `/reports`, UI đăng ký khách/trả hàng/chốt ca.
- `GET /sync/status` (FR-C10). Dữ liệu đã có trong `store_sync_status`, còn phải chốt cách xác
  thực người dùng trụ sở.

## 5. Vận hành

- Khóa `store-001` đã ghi vào `infra/.env` (dev, gitignore) dưới tên `CENTRAL_API_KEY`. Mất thì
  cấp lại: `DATABASE_URL=<central> uv run python -m central.ops.provision_store --store-id store-001`.
- Migration không chạy tự động trong compose — chạy tay `alembic ... upgrade head` với
  `DATABASE_URL` trỏ `localhost:5433` (edge) / `localhost:5434` (central).
- Không có `make` trên máy này: câu lệnh của `make sync-status` chạy thẳng bằng `docker exec`.

## 6. Sửa bẫy 1–4 của luồng dữ liệu (cùng ngày, sau khi đổi hướng)

Chủ dự án yêu cầu sửa các lỗi tiềm ẩn ở [17 §4](../17-data-flow.md) **trước** khi viết tiếp.

| Bẫy | Sửa | Tìm thêm được gì khi sửa |
|---|---|---|
| 1 — commit muộn lọt watermark | Hàm `extract_horizon()`: mốc = transaction đang mở cũ nhất, thay cho "độ trễ cố định 5 phút" của bản thiết kế. `transaction_timeout = 60s` cho mọi kết nối Central API (`make_engine(server_settings=...)`) | **Lời chặn "thiếu quyền" bản đầu vô dụng**: Postgres ẩn cả `backend_type` của phiên bị giấu, nên lọc theo cột đó trước khi đếm thì không đếm được gì, và hàm trả mốc sai mà không báo. Test bắt được. Ngoài ra `transaction_timeout` cắt bằng cách **đóng hẳn kết nối**, không trả lỗi SQL; `pool_pre_ping` lo phần hồi phục |
| 2 — thiếu index `point_ledger(recorded_at)` | Migration `0004` | — |
| 3 — UPDATE không chạm watermark | `customer.recorded_at` + **trigger** trên 4 bảng (`customer`, `sale_replica`, `shift_replica`, `point_balance`) — handler không cần nhớ, client không khai hộ được | — |
| 4 — token ClickHouse bị bỏ qua | `data_platform/clickhouse/bronze.sql`: 7 bảng bronze, mọi bảng có `non_replicated_deduplication_window` | Kiểm trên ClickHouse 24.10 thật, thấy thêm 2 điều doc chưa biết: **cùng token mà khác nội dung thì bị bỏ âm thầm** (token phải có SHA-256 nội dung), và **cửa sổ có hạn** (chạy lại quá xa → nhân đôi, phải dựng lại bằng `DROP PARTITION`, đã kiểm là đường này đi được) |
| 5 — dữ liệu trễ qua ranh giới tháng | ⏳ Chưa — nằm trong model dbt, chưa có | — |

**Kiểm chứng**
- 21 test mới (`test_extract_watermarks.py`, `test_clickhouse_bronze.py`, 1 test trong
  `test_sync_pipeline.py`). Toàn repo **286/286**; ruff, `mypy --strict`, 8/8 hợp đồng import sạch.
- **Kiểm đột biến 4/4**: mốc ngây thơ, bỏ index, bỏ trigger, bỏ `SETTINGS` ở một bảng bronze.
  Lần nào test cũng đỏ.
- **Trên compose thật:** migration `0004` chạy trên DB trung tâm đang có dữ liệu, central-api
  build lại. Xác nhận `transaction_timeout = 1min` trên kết nối của app, `extract_horizon()`
  chạy được với role `central_app`, không khách nào thiếu `recorded_at`, outbox cửa hàng không
  còn gì tồn đọng.

**Lưu ý vận hành:** role trích xuất ở production phải có `GRANT pg_read_all_stats` nếu khác
role ghi dữ liệu. Thiếu thì `extract_horizon()` từ chối chạy (có chủ đích).

## 7. Đường ghi còn thiếu cho bộ giả lập — giai đoạn A bước 1 (cùng ngày)

`POST /customers`, `POST /shifts/open`, `POST /shifts/{id}/close` (use case + route JSON,
**không UI**), seed nhân viên dựng sẵn ở cả cửa hàng lẫn trung tâm. Route tạm `/ui/shifts/open`
giờ gọi đúng use case `open_shift()`. TODO "STOPGAP" cuối cùng của tuần 1 đã gỡ.

**Quyết định trong lúc làm**
- **`CloseShift` lên Must** (ADR-010 đã sửa kèm changelog). Mỗi cửa hàng chỉ được một ca mở và
  `business_date` của đơn lấy từ ca, nên không đóng ca thì mọi đơn kẹt ở ngày mở ca đầu tiên.
  Compose thật cho thấy đúng điều đó: ca mở 17/09 chưa đóng suốt 6 ngày, nên mọi đơn tới 23/09
  (kể cả 3 đơn của lần chạy AT-02 bằng tay) mang `business_date = 17/09`.
- Mở/đóng ca đặt ở module `pos`, không đặt ở `reporting`: `reporting` bị `import-linter` khóa
  ở chế độ chỉ đọc.
- `phone_hash` = HMAC-SHA256(`PII_HASH_KEY`, SĐT đã chuẩn hóa). Không thu tên, không lưu SĐT dạng
  đọc được: chưa có khóa mã hóa ở giai đoạn A.

**Lỗi thật tìm được**

| # | Lỗi | Tìm bằng |
|---|---|---|
| 1 | Đăng ký cùng SĐT từ nhiều quầy cùng lúc → `500`. Bản CTE `INSERT ... ON CONFLICT DO NOTHING UNION SELECT` đợi bên kia commit, nhưng phần SELECT dùng snapshot đầu câu lệnh nên không thấy dòng vừa commit. Sửa: hai câu lệnh riêng | Test 8 request đồng thời |
| 2 | Race đóng ca ↔ chốt đơn: đơn đã đọc ca nhưng chưa chèn dòng `sale` thì lọt khỏi `expected_cash`. Sửa: `FOR SHARE` khi chốt đơn đọc ca, `FOR UPDATE` khi đóng ca | Đọc schema. Bản test đầu **không bắt được** khi kiểm đột biến (khóa ngoại đã che khi dòng `sale` đã chèn), nên viết lại để dừng đúng trong khe hở |
| 3 | `customer.joined_at` ở trung tâm = giờ nhận sự kiện, không phải giờ đăng ký | Viết test đầu-cuối đăng ký → đồng bộ |

**Kiểm chứng**
- 39 test mới (`test_pii.py`, `test_shifts.py`, `test_customers_shifts.py`, 1 test đầu-cuối
  trong `test_sync_pipeline.py`). Toàn repo **325/325**; ruff, `mypy --strict`, 8/8 hợp đồng sạch.
- **Kiểm đột biến 4/4:** bỏ `FOR SHARE`, `joined_at` theo giờ trung tâm, phát `CustomerCreated`
  cả khi khách đã có, bản CTE có race. Lần nào test cũng đỏ.
- **Trên compose thật** (image build lại, seed nhân viên hai phía): đóng ca cũ từ 17/09 → mở ca
  → đăng ký khách `+84 912 345 678` → 2 đơn (một đơn tách tiền mặt + thẻ) → đóng ca, `variance = 0`.
  Trung tâm nhận đủ 2 đơn, `shift_replica.expected_cash = 1.229.000`, khách có 96 điểm,
  `joined_at` là giờ đăng ký ở cửa hàng, 0 dead-letter.

**Tiếp theo (giai đoạn A):** bộ giả lập bước 1–3 ([18 §10](../18-simulator.md)), đối soát INV-4
tăng dần.

## 8. Bộ giả lập bước 1–3 + đối soát INV-4 (cùng ngày)

**Đã làm**
- **`packages/simulator/`**, không phải thư mục gốc `simulator/` như docs/18 bản đầu (đã sửa
  docs kèm lý do): profile TOML, bộ sinh thuần, sink `edge_http` (vòng hở), manifest, audit
  L0/L1/L2, CLI `run`/`audit`. Hợp đồng `import-linter` mới: bộ giả lập **không** import
  `edge`/`central`, và ngược lại.
- **`POST /sales/quote`**: tạm tính dùng CHUNG hàm `_price()` với `place_sale`. Không có nó thì
  client phải tự tính lại giá, và sẽ có hai bộ tính tiền.
- **`central.ops.reconcile`**: INV-4 tăng dần theo `extract_horizon()`, cùng định nghĩa
  `lifetime_contribution` với ingest. Lệch lưu ở bảng mới `reconciliation_drift` (migration
  `0005`) cho tới khi được sửa, và lần chạy sau xác nhận bản sửa. `--full` cho lần quét hằng tháng.
- Fixture `central`/`edge` chuyển từ `test_sync_pipeline.py` sang `conftest.py` để dùng chung.

**Lỗi/bẫy thật tìm được**

| # | Cái gì | Tìm bằng |
|---|---|---|
| 1 | ID revision Alembic dài 33 ký tự, trong khi `alembic_version.version_num` là `varchar(32)`: migration chạy hết rồi mới chết ở bước ghi phiên bản. Đã đổi tên + thêm `test_migrations.py` chặn cho mọi migration sau | Test tích hợp đầu tiên của migration 0005 |
| 2 | Bộ sinh xáo danh mục ngẫu nhiên nên mã 49 triệu có thể thành hàng bán chạy nhất: lần chạy thật đầu tiên ra **3,1 triệu/đơn**, trong khi giá trung vị danh mục là 220k. Sửa: xếp hạng phổ biến theo giá, có nhiễu → ~470k/đơn | Đọc số của lần chạy thật |
| 3 | Báo cáo đối soát lần đầu ghi `full=False` dù thực chất đã quét toàn bộ (chưa có mốc) | Chạy trên compose |

**Kiểm chứng**
- 30 test mới (bộ sinh 11, đối soát 5 + 1 qua ingest thật, bộ giả lập đầu-cuối 3, tạm tính 2,
  migration 9). Toàn repo **355/355**; ruff, `mypy --strict`, **10/10** hợp đồng sạch.
- **Kiểm đột biến 3/3:** đối soát cộng cả `RETURN` vào `lifetime`; audit coi "thiếu" luôn là
  `CONVERGING`; tạm tính bỏ chiết khấu hạng. Lần nào test cũng đỏ.
- **Test đầu-cuối:** bộ giả lập → Edge API thật → worker → Central thật → audit. Trước khi đồng
  bộ ra `CONVERGING`, sau khi đồng bộ ra `CONVERGED`. Làm hỏng có chủ đích (sửa tiền ở trung tâm,
  xóa một đơn khi outbox đã cạn) thì ra `DIVERGED`.
- **Trên compose thật**, profile `t0`, 1 cửa hàng, một ngày giả lập nén 5 phút:
  - 102 đơn, 5 khách mới, 2 ca (chênh két 0), 0 bị từ chối, 0 không rõ kết cục.
  - **L0 = L1 = L2:** 317.619.500đ, tiền mặt 195.758.500, thẻ 89.206.000, ví 32.655.000,
    19.402 điểm → `CONVERGED`.
  - Độ trễ phía client: `POST /sales` p50 70 ms · p95 111 ms · p99 126 ms (ngân sách NFR-02:
    p95 < 500 ms); `POST /sales/quote` p95 79 ms. Bộ giả lập dùng **0,5% CPU**, trễ lịch tối đa
    0,09 s, nên phép đo hợp lệ (docs/18 §1).
  - Lần hai sau khi sửa phân bố giá (nén 1 phút): 102 đơn, 55.316.000đ → `CONVERGED`.
  - `central.ops.reconcile` trên DB thật: 0 lệch.

**Tiếp theo (giai đoạn A):** profile `data` lên compose (MinIO + ClickHouse + Airflow), spike S4
(`s3()` từ MinIO), trích xuất S4 → nạp S5 → dbt S6 → audit L3/L4.

---
*Viết bởi Claude (Opus 5.5) theo các lần kiểm chứng thật trong phiên 2026-09-23.*
