# 12 — Hợp đồng sự kiện (Outbox)

> Đây là **hợp đồng ranh giới** giữa cửa hàng và trung tâm. Viết trước khi viết
> `PlaceSale` use case — sai ở đây thì store và central lệch nhau ngay từ đầu, đúng lỗi
> v1 đã mắc (4 file rỗng ở đúng 4 điểm nối, xem [09-v1-postmortem](09-v1-postmortem.md)).

---

## 1. Nguyên tắc chung

| Quy tắc | Lý do |
|---|---|
| Envelope thống nhất cho mọi loại sự kiện | Sync worker xử lý đồng nhất, không rẽ nhánh theo type |
| `event_id` là UUIDv7, sinh tại cửa hàng | Khóa idempotency — [ADR-002](adr/002-point-ledger.md), [ADR-003](adr/003-outbox-not-kafka.md) |
| Mọi sự kiện có `schema_version` | Cho phép tiến hóa mà không phá consumer cũ |
| Payload là **fact đã xảy ra**, không phải lệnh | "SaleCompleted" không phải "CreateSale" — sự kiện bất biến, xem nguyên tắc 3 ở [03-architecture](03-architecture.md) |
| `trace._traceparent` bắt buộc trong mọi payload | Nối trace store → central qua độ trễ hàng giờ — [10-observability §4](10-observability.md) |
| Không PII trong payload thô | Chỉ `customer_id` (UUID); tên/SĐT không bao giờ vào event — ràng buộc #10 |

## 2. Envelope

```jsonc
{
  "event_id": "01935e2a-...",        // UUIDv7, PK trong outbox VÀ trong processed_event
  "event_type": "SaleCompleted",     // xem bảng §3
  "schema_version": 1,
  "store_id": "store-042",
  "occurred_at": "2026-09-11T03:12:44.102Z",   // UTC, tại cửa hàng
  "payload": { /* xem từng loại bên dưới */ },
  "trace": {
    "traceparent": "00-4bf92f...-1"  // W3C, inject() lúc ghi, extract() lúc worker xử lý
  }
}
```

`recorded_at` **không** nằm trong envelope — trung tâm tự gán `now()` khi nhận, dùng để
phân vùng bronze (ràng buộc #3) và phát hiện lệch đồng hồ.

Envelope dựng ở **một chỗ duy nhất**: `shared.events.build_envelope()`. Outbox thật
(`shared.outbox.enqueue_event`) và cửa hàng ảo của bộ giả lập ([18 §3](18-simulator.md)) cùng
gọi hàm này, nên không thể lệch nhau ở một field nào đó.

## 3. Các loại sự kiện

### 3.1. `SaleCompleted`

Sinh ra khi `sale.status = COMPLETED`, trong **cùng transaction** với ghi `sale`.

```jsonc
{
  "sale_id": "01935e2a-...",
  "shift_id": "01935e1c-...",
  "employee_id": "emp-007",
  "customer_id": "01935abc-..." | null,
  "business_date": "2026-09-11",
  "subtotal": 560000, "discount_tier": 28000, "discount_promo": 0, "total": 532000,
  "tendered_amount": 550000, "change_amount": 18000,
  "promotion_id": "promo-2026-09" | null,
  "lines": [
    { "line_no": 1, "product_id": "SKU-001", "quantity": 2, "unit_price": 150000, "line_total": 300000 }
  ],
  "payments": [
    { "seq": 1, "method": "CASH", "amount": 300000 },
    { "seq": 2, "method": "CARD", "amount": 232000, "reference": "VISA-...1234" }
  ]
}
```

> ⚠️ **`lines` và `payments` đi kèm trong cùng sự kiện**, không phải 3 sự kiện riêng. Trung
> tâm ghi cả ba bảng (`sale_replica`, `sale_line_replica`, `sale_payment_replica`) từ một
> lần dedupe theo `event_id` — tránh race giữa các sự kiện con.

### 3.2. `SaleReturned`

Sinh khi trả hàng (một phần hoặc toàn bộ). **Không sửa `SaleCompleted` gốc.**

```jsonc
{
  "sale_id": "01935f10-...",              // ID của giao dịch trả (mới)
  "voided_by_sale_id": null,
  "original_sale_id": "01935e2a-...",     // đơn gốc bị trả
  "lines": [
    { "line_no": 1, "product_id": "SKU-001", "quantity": -1,
      "original_sale_id": "01935e2a-...", "original_sale_line_no": 1 }
  ],
  "reason": "CUSTOMER_RETURN"
}
```

### 3.3. `PointsEarned` / `PointsReturned` / `PointsAdjusted`

Ánh xạ gần 1-1 từ một dòng `point_ledger`. `reason` quyết định type.

```jsonc
{
  "customer_id": "01935abc-...",
  "sale_id": "01935e2a-..." | null,
  "delta": 53,                    // dương với Earned, âm với Returned/Adjusted
  "reason": "EARN",               // EARN | RETURN | ADJUST | EXPIRE
  "metadata": {}
}
```

> `event_id` của sự kiện này **chính là** `point_ledger.event_id` ở cả hai đầu (cục bộ và
> trung tâm) — không sinh ID mới khi chuyển tiếp.

### 3.4. `CustomerCreated` / `CustomerUpdated`

```jsonc
{
  "customer_id": "01935abc-...",
  "phone_hash": "…",         // chuỗi opaque, trung tâm lưu nguyên và dùng để tra/dò trùng
  "phone_enc": "q83vEjRW…",  // base64 chuẩn (RFC 4648) của ciphertext, KHÔNG có tiền tố
  "name_enc": "q83vEjRW…",   // như trên; null nếu chưa có
  "created_locally_at_store": "store-042" | null   // C02: tạo khi offline
}
```

`*_enc` không phải base64 hợp lệ → trung tâm từ chối vĩnh viễn (`retryable=false`).

**`phone_hash` = HMAC-SHA256(`PII_HASH_KEY`, SĐT đã chuẩn hóa về dạng `0xxxxxxxxx`)**, hex
(`shared/pii.py`, 2026-09-23). Chuẩn hóa trước khi băm là bắt buộc: `0901 234 567` và
`+84901234567` phải ra cùng một hash, nếu không C03 không bao giờ dò ra trùng. Khóa HMAC
**giống nhau ở mọi cửa hàng và trung tâm**. SHA-256 trần thì dò ngược được hết 10⁹ số VN
trong vài phút. Ở giai đoạn A, `phone_enc` và `name_enc` luôn là `null`: chưa có khóa mã
hóa, nên cửa hàng không thu tên và không lưu SĐT dạng đọc được (ràng buộc #10).

`occurred_at` của `CustomerCreated` là **lúc đăng ký tại cửa hàng**. Trung tâm dùng nó làm
`customer.joined_at`, không dùng giờ nhận sự kiện: cửa hàng offline 3 ngày thì ngày gia nhập
không được lệch 3 ngày.

**Trùng `phone_hash` (case C03) KHÔNG phải lỗi:** hai cửa hàng offline có thể cùng tạo khách
cho một SĐT. Trung tâm ghi cả hai `customer_id` và đưa cặp vào `customer_duplicate_candidate`
để gộp thủ công — từ chối bản thứ hai sẽ làm điểm của khách đó vỡ khóa ngoại và mất khỏi
trung tâm. `CustomerCreated` trùng `customer_id` đã có thì bỏ qua (cùng ID = cùng khách).

### 3.5. `ShiftClosed`

```jsonc
{
  "shift_id": "01935e1c-...",
  "business_date": "2026-09-11",
  "opened_by_employee_id": "emp-007", "closed_by_employee_id": "emp-009",
  "opened_at": "...", "closed_at": "...",
  "opening_cash": 500000, "expected_cash": 2140000,
  "counted_cash": 2120000, "variance": -20000, "variance_note": "chưa rõ nguyên nhân"
}
```

## 4. Versioning

Quy tắc tiến hóa (bắt buộc tuân thủ để không phá consumer đang chạy):

| Thay đổi | Có cần tăng `schema_version`? |
|---|---|
| Thêm field mới, optional | ❌ Không — consumer cũ bỏ qua field lạ |
| Đổi kiểu dữ liệu của field | ✅ Có |
| Xóa field | ✅ Có |
| Đổi ý nghĩa field (không đổi tên/kiểu) | ✅ Có — đây là lỗi âm thầm nguy hiểm nhất |

Central API xử lý theo `schema_version`: version chưa hỗ trợ → ghi vào dead-letter, cảnh
báo, **không** cố đoán ý nghĩa.

## 5. Idempotency ở đầu nhận

```sql
INSERT INTO processed_event (event_id, received_at) VALUES (:event_id, now())
ON CONFLICT (event_id) DO NOTHING;
-- Chỉ xử lý payload nếu INSERT thực sự chèn được dòng mới (rowcount = 1)
```

Áp dụng đồng nhất cho mọi `event_type` — không có ngoại lệ, không có type nào "chắc chắn
không lặp" nên được bỏ qua bước này.

---
*Changelog: 2026-09-24 — §2: envelope dựng bằng `build_envelope()` dùng chung.*

*Changelog: 2026-09-23 — §3.4: công thức `phone_hash` (HMAC + chuẩn hóa SĐT), `*_enc` null ở
giai đoạn A, `occurred_at` → `joined_at`.*

*Changelog: 2026-09-11 — tạo mới.*
