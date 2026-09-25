# 13 — Hợp đồng API

> Route + request/response ở mức đủ để viết use case và test tích hợp. OpenAPI YAML chính
> thức sinh từ FastAPI (tự động), doc này là **bản thiết kế trước khi có code**.

---

## 1. Edge API (tại cửa hàng)

Base path `/api/v1`. Mọi route (trừ `/auth/login`, `/health`) yêu cầu JWT, token mang
`store_id` + `employee_id` + `role`.

> 🔀 **Thứ tự làm theo [ADR-010](adr/010-data-flow-first.md).** Hợp đồng dưới đây là của sản
> phẩm cuối và **không đổi**. Chỉ thứ tự làm thay đổi:
>
> | Giai đoạn | Route |
> |---|---|
> | ✅ Đã có | `POST /auth/login`, `GET /products`, `POST /sales`, `GET /health`, `GET /health/outbox` · Central `POST /events` |
> | ✅ **A** (2026-09-23, **không kèm UI**) | `POST /customers`, `POST /shifts/open`, `POST /shifts/{id}/close` — hợp đồng chi tiết ở §5 · `POST /sales/quote` — §3 |
> | **A — Should** | `POST /returns` |
> | **C** | `GET /customers?phone=`, `POST /sales/{id}/void`, `/shifts/{id}/report`, `/reports/*` · Central `lookup`, `reporting`, `/sync/status` |

### Module `pos`

| Method | Path | Vai trò tối thiểu | Ghi chú |
|---|---|---|---|
| `POST` | `/auth/login` | — | FR-P01. Trả access (15p) + refresh token |
| `GET` | `/products?barcode=` | cashier | FR-P03. Đọc `product_cache`, p95 < 100ms |
| `POST` | `/sales/quote` | cashier | FR-P05. Tạm tính, không ghi gì. ✅ §3 |
| `POST` | `/sales` | cashier | FR-P04–P07. Xem §3 — luồng chính |
| `POST` | `/sales/{sale_id}/void` | manager | FR-P09 |
| `POST` | `/returns` | cashier | FR-P15. Body: `original_sale_id`, danh sách dòng trả |
| `GET` | `/customers?phone=` | cashier | FR-L01. Cache → local → central (§4) |
| `POST` | `/customers` | cashier | FR-L02. Tạo khi offline được (`created_locally=true`). ✅ §5 |
| `POST` | `/shifts/open` | cashier | FR-P11. ✅ §5 — **chuyển từ `reporting` sang `pos`**, xem dưới |
| `POST` | `/shifts/{shift_id}/close` | manager | FR-P11. ✅ §5 |

### Module `loyalty` *(nội bộ — không expose route riêng ở v1)*

Gọi qua `LoyaltyPort` từ trong use case `PlaceSale`, không qua HTTP (modular monolith,
[ADR-004](adr/004-modular-monolith.md)). Không có route công khai.

### Module `reporting` 🆕

| Method | Path | Vai trò tối thiểu | Ghi chú |
|---|---|---|---|
| `GET` | `/shifts/{shift_id}/report` | manager | FR-R01. Quét < 1000 dòng, T+0 |

> Mở/đóng ca **đã chuyển sang module `pos`** (2026-09-23). `reporting` bị `import-linter`
> khóa ở chế độ chỉ đọc (không được gọi use case ghi), còn mở/đóng ca là thao tác ghi và phát
> `ShiftClosed`. `reporting` giữ các route báo cáo ca, chỉ đọc.
| `GET` | `/reports/today` | manager | FR-R02. Doanh thu hôm nay, theo `business_date` |
| `GET` | `/reports/by-cashier?shift_id=` | manager | FR-R03 |

### Health & sync

| Method | Path | Ghi chú |
|---|---|---|
| `GET` | `/health` | Không auth. Kiểm tra kết nối PG + Redis (Redis lỗi vẫn `200`, có cờ `redis: degraded`) |
| `GET` | `/health/outbox` | Trả `outbox_depth`, `oldest_unsent_age_seconds` — dùng cho metric [08 §5](08-reliability-and-scale.md) |

---

## 2. Central API (trung tâm)

Base path `/api/v1`. Mọi route yêu cầu API key theo cửa hàng (không dùng JWT người dùng ở
tầng này — đây là service-to-service).

### Module `ingest`

| Method | Path | Ghi chú |
|---|---|---|
| `POST` | `/events` | Nhận lô sự kiện từ sync worker. Body: mảng envelope §[12](12-event-schema.md). Trả `{accepted: [event_id...], rejected: [{event_id, reason, retryable}...]}` |

**Ứng xử khi nhận lô:** xử lý **từng sự kiện độc lập** trong lô — một sự kiện lỗi không
được làm rớt cả lô (worker sẽ coi cả lô là "chưa gửi" và retry, gây lặp vô ích cho các sự
kiện đã thành công). Hiện thực: một transaction cho lô, một SAVEPOINT cho mỗi sự kiện.

**Xác thực:** `Authorization: Bearer <khóa cửa hàng>`, cấp bằng `central.ops.provision_store`.
`store_id` trong TỪNG envelope phải trùng cửa hàng sở hữu khóa — sai thì sự kiện đó bị từ
chối (`store_mismatch`, không thử lại).

**`retryable`** — quyết định số phận sự kiện ở cửa hàng:

| `retryable` | Khi nào | Worker làm gì |
|---|---|---|
| `false` | envelope/payload sai, `schema_version` lạ, sai cửa hàng, vi phạm unique/check | Dead-letter ngay, không đốt lượt thử. Trung tâm lưu bản sao vào `dead_letter_event` |
| `true` | vỡ khóa ngoại (sự kiện phụ thuộc chưa tới), loại sự kiện chưa có handler, lỗi DB tạm | Lùi theo `next_attempt_at` của riêng sự kiện; dead-letter sau `SYNC_MAX_ATTEMPTS_BEFORE_DEAD_LETTER` lần |

Vi phạm check gồm cả **"no partition for row" (23514)** của `point_ledger`: sự kiện điểm có
`occurred_at` ngoài mọi partition. Từ migration `0006` (2026-09-24) luôn có partition tháng
trước, nên cửa hàng offline qua cuối tháng không còn rơi vào đây; chỉ đồng hồ sai hoặc dữ liệu
cũ hơn một tháng mới bị từ chối ([05 §4.3](05-data-model.md)).

Thiếu field này (trung tâm cũ) → mặc định `true`: thà gửi lại thừa còn hơn vứt nhầm.
Sự kiện đã xử lý ở lần gửi trước vẫn trả về trong `accepted` (AT-03).

**Lỗi cấp request** — worker coi là lỗi đường truyền, **không** trừ lượt thử của sự kiện nào:

| Mã | Khi nào |
|---|---|
| `401` | Thiếu khóa, khóa sai, hoặc đã thu hồi |
| `413` | Lô vượt `INGEST_MAX_BATCH` (mặc định 500) |
| `503` + `Retry-After` | Vượt `INGEST_MAX_CONCURRENCY` request đồng thời (docs/08 §3.2) |

`SaleReturned` **chưa có handler** (payload ở docs/12 §3.2 thiếu cột bắt buộc của
`sale_replica`) — trả `retryable=true` để sự kiện chờ chứ không mất. Bổ sung khi làm trả hàng.

### Module `lookup`

| Method | Path | Ghi chú |
|---|---|---|
| `GET` | `/customers/{customer_id}` | FR-L06. Trả hồ sơ + `point_balance` (balance, lifetime_earned, tier) |
| `GET` | `/customers/by-phone?phone_hash=` | Cửa hàng tra khách mới bằng SĐT |
| `GET` | `/master-data?since_version=` | FR-C01. Trả delta kể từ `synced_version` cửa hàng đang có |

**Timeout hợp đồng:** route `/customers/*` phải trả lời **hoặc timeout ở phía server sau
300ms** (thấp hơn ngân sách 500ms của client, để dư thời gian cho network round-trip).
Không được là route "chờ tùy ý" — nếu chậm, trả lỗi rõ ràng hơn là để client tự timeout.

### Module `reporting`

| Method | Path | Ghi chú |
|---|---|---|
| `GET` | `/reports/stores/{store_id}?from=&to=` | FR-R04, T+0/T+1, đọc thẳng bảng replica |
| `GET` | `/reports/region/{region_id}/compare` | FR-R05 |
| `GET` | `/sync/status` | FR-C10. `store_sync_lag_seconds` theo từng cửa hàng |

---

## 3. Luồng `POST /sales` — hợp đồng chi tiết

Đây là route quan trọng nhất; ghi lại đầy đủ vì nó là ranh giới giữa UI và toàn bộ logic
nghiệp vụ.

**Request:**
```jsonc
{
  "employee_id": "emp-007", "customer_id": "01935abc-..." | null,
  "shift_id": "01935e1c-...",
  "lines": [{ "product_id": "SKU-001", "quantity": 2 }],
  "payments": [{ "method": "CASH", "amount": 300000 }],
  "tendered_amount": 300000
}
```

**Response `201`:**
```jsonc
{
  "sale_id": "01935e2a-...", "status": "COMPLETED",
  "subtotal": 300000, "discount_tier": 15000, "discount_promo": 0, "total": 285000,
  "change_amount": 15000, "tier_discount_pct": 5,
  "points_earned": 28   // hiển thị ngay, dù ledger trung tâm chưa xác nhận
}
```

**Lỗi:**

| Mã | Khi nào | Có rollback transaction không? |
|---|---|---|
| `400` | Giỏ hàng rỗng, `SUM(payments) != total` | Có — chưa insert gì |
| `404` | `product_id` không có trong `product_cache` | Có |
| `409` | `shift_id` đã đóng | Có |
| `500` | Lỗi DB không mong đợi | Có — Postgres tự rollback |

**Tạm tính — `POST /sales/quote`** *(2026-09-23)*. Body giống `/sales` nhưng chỉ có `lines`,
`customer_id`, `promo_discount_pct`. Trả `{subtotal, discount_tier, discount_promo, total,
tier_discount_pct}` và không ghi gì. Dùng **chung hàm tính tiền** với `/sales`: trả đúng `total`
thì đơn qua. Lỗi `404`/`400` giống `/sales`. Không cần ca đang mở.

**Không có mã lỗi nào cho "trung tâm không tới được"** — vì luồng chốt đơn **không bao giờ
gọi trung tâm đồng bộ**. Tra cứu khách (nếu cache/local miss) xảy ra *trước* khi vào
transaction chốt đơn, và tự nó không có lỗi ra ngoài (timeout → coi như khách ẩn danh với
hạng mặc định, ghi log cảnh báo).

---

## 4. Hợp đồng tra cứu khách hàng — thứ tự thác đổ (fallback chain)

```
1. Redis GET customer:{phone_hash}         → hit: dùng ngay (TTL 15p)
2. Postgres SELECT customer_local          → hit: set cache, dùng
3. GET central /customers/by-phone         → timeout 500ms
     hit:  set cache + customer_local, dùng
     miss/timeout: customer_id = null, tier = "chưa xác định", KHÔNG chặn đơn hàng
```

Route nội bộ này **không** có mã lỗi trả ra UI cho nhánh (3) thất bại — nó là fallback nội
bộ của use case `ResolveCustomer`, luôn trả về một kết quả (có thể là "ẩn danh").

---

## 5. Khách hàng & ca — hợp đồng chi tiết *(2026-09-23)*

Ba route này là nguồn dữ liệu còn thiếu của luồng cho bộ giả lập (ADR-010 quy tắc 2).

### `POST /customers` — cashier

| | |
|---|---|
| Body | `{"phone": "0901 234 567"}` — **chỉ SĐT**. Nhận `+84`, `84`, `0084`, dấu cách/chấm/gạch/ngoặc. Không nhận tên ở giai đoạn A |
| `201` | `{"customer_id": "…", "created": true}` — tạo `customer_local` + `CustomerCreated` vào outbox, một transaction |
| `200` | `{"customer_id": "…", "created": false}` — SĐT đã có ở cửa hàng này (khách quen). **Không** phát thêm sự kiện |
| `422` | `{"code": "INVALID_PHONE"}` — thông điệp không chứa số đã gõ |

Nhiều quầy cùng đăng ký một SĐT cùng lúc → đúng một `201`, còn lại `200` cùng `customer_id`.

### `POST /shifts/open` — cashier

| | |
|---|---|
| Body | `{"business_date": "2026-09-23", "opening_cash": 500000}` — `business_date` **bắt buộc** (G5) và phải là hôm nay hoặc hôm qua theo giờ cửa hàng (`STORE_UTC_OFFSET_MINUTES`, mặc định 420) |
| `201` | `{"shift_id": "…", "business_date": "…"}` |
| `409` | `{"code": "SHIFT_ALREADY_OPEN", "shift_id": "…"}` — mỗi cửa hàng tối đa MỘT ca mở; trả kèm ca đang mở để client dùng tiếp hoặc đóng |
| `422` | `INVALID_BUSINESS_DATE`, `INVALID_OPENING_CASH` |

### `POST /shifts/{shift_id}/close` — manager

| | |
|---|---|
| Body | `{"counted_cash": 2120000, "variance_note": "…" \| null}` |
| `200` | `{shift_id, business_date, opening_cash, cash_collected, expected_cash, counted_cash, variance, closed_at}` + `ShiftClosed` vào outbox |
| `409` | `SHIFT_CLOSED` — ca đã đóng, không tồn tại, hoặc thuộc cửa hàng khác |
| `403` | Thu ngân gọi (cần `manager`) |

`cash_collected` = Σ phần **tiền mặt** của các đơn trong ca, không phải tiền khách đưa. Đơn
đang chốt dở giữ `FOR SHARE` trên ca, còn đóng ca lấy `FOR UPDATE`: đóng ca **đợi** đơn đó
commit rồi mới cộng. Đơn tới sau thì bị `409 SHIFT_CLOSED` (docs/14 §3).

---
*Changelog: 2026-09-23 (lần 2) — §5 hợp đồng `/customers`, `/shifts/open`, `/shifts/{id}/close`
(đã hiện thực); mở/đóng ca chuyển từ module `reporting` sang `pos`.*

*Changelog: 2026-09-24 — §2: ghi rõ 23514 (no partition) và bản sửa `0006`.*

*Changelog: 2026-09-23 — thêm bảng thứ tự làm theo ADR-010 (hợp đồng không đổi).*

*Changelog: 2026-09-11 — tạo mới.*
