# 13 — Hợp đồng API

> Route + request/response ở mức đủ để viết use case và test tích hợp. OpenAPI YAML chính
> thức sinh từ FastAPI (tự động), doc này là **bản thiết kế trước khi có code**.

---

## 1. Edge API (tại cửa hàng)

Base path `/api/v1`. Mọi route (trừ `/auth/login`, `/health`) yêu cầu JWT, token mang
`store_id` + `employee_id` + `role`.

### Module `pos`

| Method | Path | Vai trò tối thiểu | Ghi chú |
|---|---|---|---|
| `POST` | `/auth/login` | — | FR-P01. Trả access (15p) + refresh token |
| `GET` | `/products?barcode=` | cashier | FR-P03. Đọc `product_cache`, p95 < 100ms |
| `POST` | `/sales` | cashier | FR-P04–P07. Xem §3 — luồng chính |
| `POST` | `/sales/{sale_id}/void` | manager | FR-P09 |
| `POST` | `/returns` | cashier | FR-P15. Body: `original_sale_id`, danh sách dòng trả |
| `GET` | `/customers?phone=` | cashier | FR-L01. Cache → local → central (§4) |
| `POST` | `/customers` | cashier | FR-L02. Tạo khi offline được (`created_locally=true`) |

### Module `loyalty` *(nội bộ — không expose route riêng ở v1)*

Gọi qua `LoyaltyPort` từ trong use case `PlaceSale`, không qua HTTP (modular monolith,
[ADR-004](adr/004-modular-monolith.md)). Không có route công khai.

### Module `reporting` 🆕

| Method | Path | Vai trò tối thiểu | Ghi chú |
|---|---|---|---|
| `POST` | `/shifts/open` | cashier/manager | FR-P11. Ghi `shift`, trả `shift_id` |
| `POST` | `/shifts/{shift_id}/close` | manager | Ghi `counted_cash`, tính `variance` |
| `GET` | `/shifts/{shift_id}/report` | manager | FR-R01. Quét < 1000 dòng, T+0 |
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
| `POST` | `/events` | Nhận lô sự kiện từ sync worker. Body: mảng envelope §[12](12-event-schema.md). Trả `{accepted: [event_id...], rejected: [{event_id, reason}...]}` |

**Ứng xử khi nhận lô:** xử lý **từng sự kiện độc lập** trong lô — một sự kiện lỗi không
được làm rớt cả lô (worker sẽ coi cả lô là "chưa gửi" và retry, gây lặp vô ích cho các sự
kiện đã thành công).

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
*Changelog: 2026-09-11 — tạo mới.*
