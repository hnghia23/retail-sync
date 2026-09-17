# ADR-002 — Điểm tích lũy dùng ledger append-only

**Trạng thái:** Được chấp nhận · 2026-09-11
**Mức độ quan trọng:** Cao nhất trong bộ ADR. Đây là quyết định định hình cả hệ thống loyalty.

## Bối cảnh

Thiết kế v1 lưu điểm dưới dạng **số dư có thể cập nhật**:

```sql
-- Postgres cửa hàng
loyalty_customer(customer_id, tier, point, total_money_used, last_updated)
-- Cassandra trung tâm
customers_point(customer_id, tier, points, total_money_used)
```

và cập nhật bằng `point = point + earned`.

Mô hình này hỏng theo ba cách, và cả ba đều xảy ra trong vận hành bình thường:

### Hỏng 1 — Mất cập nhật khi ghi đồng thời

```
t0  Cửa hàng A đọc số dư = 100
t0  Cửa hàng B đọc số dư = 100
t1  A ghi 100 + 50 = 150
t2  B ghi 100 + 30 = 130     ← đè lên A. Mất 50 điểm của khách.
```

Không phải trường hợp hiếm — chỉ cần khách mua ở hai nơi trong cùng khoảng đồng bộ.

### Hỏng 2 — Hai nguồn sự thật

Điểm nằm ở cả Postgres cửa hàng lẫn Cassandra trung tâm, cả hai đều ghi đè được. Khi lệch
nhau, **không có cách nào biết cái nào đúng** — vì không lưu lại chuyện gì đã xảy ra.

### Hỏng 3 — Không kiểm toán được

Khách khiếu nại "tôi bị mất điểm". Với mô hình số dư, không trả lời được, vì không có lịch
sử. Với dữ liệu có giá trị như tiền, điều này không chấp nhận được (NFR-05).

## Quyết định

Điểm tích lũy được mô hình hóa bằng **sổ cái chỉ ghi thêm (append-only ledger)**. Số dư
là **kết quả suy ra**, không phải dữ liệu được cập nhật.

```sql
CREATE TABLE point_ledger (
    event_id        uuid        PRIMARY KEY,   -- sinh tại cửa hàng = khóa idempotency
    customer_id     text        NOT NULL,
    store_id        text        NOT NULL,
    transaction_id  text,                      -- đơn hàng nguồn (nếu có)
    delta           integer     NOT NULL,      -- dương = tích, âm = tiêu/trả/hết hạn
    reason          text        NOT NULL,      -- EARN | REDEEM | RETURN | EXPIRE | ADJUST
    occurred_at     timestamptz NOT NULL,      -- thời điểm xảy ra ở cửa hàng
    recorded_at     timestamptz NOT NULL DEFAULT now(),  -- thời điểm trung tâm nhận
    metadata        jsonb
);

CREATE INDEX ON point_ledger (customer_id, occurred_at DESC);
```

**Không bao giờ UPDATE. Không bao giờ DELETE.** Sửa sai bằng cách ghi thêm dòng bù trừ
(`reason = 'ADJUST'`).

Số dư:
```sql
SELECT COALESCE(SUM(delta), 0) FROM point_ledger WHERE customer_id = $1;
```

Kèm bảng snapshot vật chất hóa cho tốc độ đọc:
```sql
CREATE TABLE point_balance (
    customer_id   text PRIMARY KEY,
    balance       integer     NOT NULL,
    lifetime_earned integer   NOT NULL,   -- dùng để xếp hạng (không giảm khi tiêu điểm)
    tier          text        NOT NULL,
    last_event_at timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
```
Snapshot là **cache có thể dựng lại**, không phải nguồn sự thật.

## Vì sao cách này giải quyết được cả ba vấn đề

| Vấn đề | Cách ledger xử lý |
|---|---|
| Ghi đồng thời | Hai cửa hàng ghi **hai dòng khác nhau**. Không có ô nào bị đè. `SUM` luôn đúng bất kể thứ tự đến |
| Hai nguồn sự thật | Chỉ có một: tập hợp các dòng ledger. Dòng cửa hàng và dòng trung tâm **hợp nhất** được vì mỗi dòng có `event_id` duy nhất |
| Không kiểm toán | Mọi thay đổi đều có dòng riêng kèm nguyên nhân, nguồn gốc, thời điểm. Trả lời được mọi khiếu nại |
| Đồng bộ lặp | `INSERT ... ON CONFLICT (event_id) DO NOTHING`. Gửi 100 lần vẫn chỉ ghi 1 dòng |
| Đến trễ, sai thứ tự | Cộng có tính giao hoán. Dòng đến sau 3 ngày vẫn cộng đúng |
| Trả hàng | Ghi dòng `delta` âm, `reason = 'RETURN'`. Đơn gốc vẫn nguyên |
| Điểm hết hạn | Job định kỳ ghi dòng âm. Không cần logic đặc biệt |
| Gộp khách trùng | `UPDATE point_ledger SET customer_id = ...` — số dư tự đúng |

**Đây là lý do ledger không chỉ "tốt hơn" mà là mô hình duy nhất đúng cho bài toán này:**
một hệ thống phân tán, offline-first, ghi từ nhiều nơi, cần kiểm toán. Ledger biến vấn đề
đồng thuận phân tán thành một phép cộng có tính giao hoán.

## Chi tiết triển khai

### Hai tầng ledger

```
point_ledger CỤC BỘ (Postgres cửa hàng)
    → ghi ngay khi bán, trong cùng transaction với đơn hàng
    → phục vụ hiển thị tức thì cho khách
    → là bằng chứng để phát lại nếu đồng bộ mất

point_ledger TRUNG TÂM (Postgres trung tâm)  ← NGUỒN SỰ THẬT
    → nhận từ mọi cửa hàng, dedupe theo event_id
    → nuôi snapshot point_balance
```

### Tránh hiển thị số dư tụt lùi

Khi cửa hàng lấy số dư từ trung tâm, phải cộng thêm các dòng cục bộ **chưa gửi**:

```
số dư hiển thị = số dư trung tâm + SUM(ledger cục bộ có event_id chưa được xác nhận)
```

Nếu không, khách vừa thấy 150 điểm sẽ thấy tụt về 100 khi đồng bộ chưa xong.

### Xếp hạng dựa trên `lifetime_earned`, không phải `balance`

Nếu xếp hạng theo số dư hiện tại, khách tiêu điểm sẽ bị **tụt hạng** — trải nghiệm rất tệ
và gây khiếu nại. Xếp hạng phải dựa trên tổng điểm từng tích:

```sql
lifetime_earned = SUM(delta) WHERE delta > 0 AND reason = 'EARN'
```

### Đối soát (NFR-05, AT-10)

Job hằng ngày:
```sql
SELECT b.customer_id, b.balance, SUM(l.delta) AS tinh_lai
FROM point_balance b
JOIN point_ledger l USING (customer_id)
GROUP BY b.customer_id, b.balance
HAVING b.balance <> SUM(l.delta);
```
Kết quả phải rỗng. Khác rỗng = cảnh báo ngay.

## Phương án đã xem xét và loại

| Phương án | Vì sao loại |
|---|---|
| Số dư + khóa bi quan (`SELECT FOR UPDATE`) | Đúng về mặt kỹ thuật, nhưng cần khóa **xuyên mạng** giữa cửa hàng và trung tâm → vi phạm nguyên tắc 2 (không khóa qua ranh giới mạng) và làm chết tính offline |
| Số dư + optimistic locking (version) | Xử lý được xung đột nhưng chỉ ở một DB. Không giải quyết được hợp nhất giữa cửa hàng và trung tâm. Và vẫn không kiểm toán được |
| CRDT counter | Về lý thuyết phù hợp (cộng giao hoán) nhưng phức tạp hơn ledger mà không thêm lợi ích nào ở đây. Ledger *chính là* một G-Counter đơn giản, dễ hiểu, lại kiểm toán được |
| Cassandra counter column | Cộng được đồng thời, nhưng không đọc chính xác được, không kiểm toán được, và kéo theo cả Cassandra ([ADR-001](001-postgres-everywhere.md)) |
| Event sourcing toàn phần cho mọi entity | Đúng cho điểm, nhưng thừa cho đơn hàng và sản phẩm. Áp dụng có chọn lọc |

## Hệ quả

### Tích cực
- Không bao giờ mất điểm do ghi đua
- Kiểm toán đầy đủ — xử lý được mọi khiếu nại
- Idempotent tự nhiên
- Tính năng tương lai (hết hạn, gộp khách, chiến dịch, hoàn điểm) gần như miễn phí
- Đến trễ và sai thứ tự đều đúng

### Tiêu cực
- **Bảng ledger lớn hơn nhiều** so với bảng số dư: 1 dòng mỗi giao dịch thay vì 1 dòng mỗi
  khách. Ở T3 là ~584 Tr dòng/năm → **bắt buộc phân vùng theo tháng** từ T2
- Phải duy trì snapshot đồng bộ với ledger → cần job đối soát
- Đọc số dư phức tạp hơn một chút (ưu tiên snapshot, fallback tính tổng)
- Lập trình viên quen mô hình CRUD sẽ thấy lạ — cần ghi rõ trong docs (đang làm đây)

### Rủi ro cần canh
- Nếu snapshot lệch mà không ai phát hiện → khách thấy sai điểm. **Job đối soát là bắt
  buộc, không phải tùy chọn.**
- Phân vùng phải làm **trước khi** bảng quá lớn, không phải sau.

## Điều kiện xem lại

Quyết định này gần như không nên đảo. Nếu có, chỉ khi: bỏ hoàn toàn yêu cầu offline **và**
chỉ còn một cửa hàng — lúc đó ledger là thừa.
