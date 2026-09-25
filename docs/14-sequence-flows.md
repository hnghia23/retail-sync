# 14 — Sơ đồ tuần tự: 4 luồng còn lại

> [03-architecture.md §4](03-architecture.md) đã vẽ luồng **bán hàng**. Đây là 4 luồng còn
> lại — thiếu chúng dễ bỏ sót case khi code, vì mỗi luồng chạm một ràng buộc khác nhau.

---

## 1. Trả hàng (một phần)

```mermaid
sequenceDiagram
    participant C as Thu ngân
    participant A as Edge API
    participant P as Postgres (cửa hàng)

    C->>A: Tra đơn cũ theo SĐT khách (không có hóa đơn giấy)
    A->>P: SELECT sale WHERE customer_id=? ORDER BY occurred_at DESC
    Note over P: Dùng index (customer_id, occurred_at DESC) — FR-P17
    P-->>A: Danh sách đơn + dòng sản phẩm
    C->>A: Chọn 2/5 dòng để trả

    rect rgb(230, 245, 235)
        Note over A,P: MỘT TRANSACTION
        A->>P: INSERT sale (loại RETURN, original_sale_id=...)
        A->>P: INSERT sale_line (quantity ÂM, original_sale_line_no=...)
        Note over P: CHECK: SUM(số lượng trả) <= số lượng đã mua dòng gốc (INV-5)
        A->>P: INSERT point_ledger_local (delta ÂM, reason=RETURN)
        Note over A: Q-B5: không tụt hạng trong kỳ (config ReturnRules)
        A->>P: INSERT outbox (SaleReturned, PointsReturned)
        P-->>A: COMMIT
    end
    A-->>C: Hóa đơn trả hàng + điểm bị trừ
```

**Điểm khác biệt với luồng bán:** không gọi trung tâm đồng bộ nào ở đây — trả hàng hoàn
toàn cục bộ, giống bán hàng. Case R04 (trả ở cửa hàng khác nơi mua) **không** được hỗ trợ ở
v1 vì cần dữ liệu đơn gốc mà cửa hàng B không có khi offline ([ADR-008 C4](adr/008-remaining-decisions.md) áp dụng tinh thần tương tự).

---

## 2. Kéo master data (Trung tâm → Cửa hàng)

```mermaid
sequenceDiagram
    participant S as Scheduler (cron nội bộ Edge API)
    participant A as Edge API
    participant X as Central API
    participant P as Postgres (cửa hàng)

    loop mỗi 5 phút (hoặc khi mở ca)
        S->>A: trigger pull_master_data()
        A->>P: SELECT last_version FROM sync_state
        A->>X: GET /master-data?since_version=42
        alt Có thay đổi
            X-->>A: [{resource: product, version: 43, rows: [...]}, ...]
            A->>P: UPSERT product_cache, promotion_cache, tier_rule_cache...
            A->>P: UPDATE sync_state SET last_version=43, last_synced_at=now()
        else Không đổi gì
            X-->>A: 304 / rows rỗng
        end
        alt Trung tâm không tới được
            Note over A: Giữ nguyên cache cũ. Nếu now()-last_synced_at > 24h → cảnh báo (B02 E03)
        end
    end
```

**Ràng buộc:** cửa hàng **chủ động kéo**, trung tâm không bao giờ đẩy — vì cửa hàng
thường sau NAT, trung tâm không có đường gọi vào (xem [ADR-003](adr/003-outbox-not-kafka.md)
lý do tương tự cho hướng ngược lại).

---

## 3. Chốt ca

```mermaid
sequenceDiagram
    participant M as Quản lý cửa hàng
    participant A as Edge API (module reporting)
    participant P as Postgres (cửa hàng)

    M->>A: POST /shifts/{id}/close {counted_cash: 2120000}
    A->>P: SELECT shift ... FOR UPDATE
    Note over P: ĐỢI mọi đơn đang chốt dở (chúng giữ FOR SHARE trên ca)
    A->>P: SELECT SUM(sale_payment.amount) WHERE method='CASH' AND shift_id=?
    A->>P: expected_cash = opening_cash + tiền_mặt_thu
    A->>A: variance = counted_cash - expected_cash
    A->>P: UPDATE shift SET closed_at=now(), counted_cash=?, expected_cash=?, variance=?, status='CLOSED'
    Note over A,P: KHÔNG sửa bất kỳ sale nào để "cân" số liệu — variance là sự thật, ghi lại nguyên trạng
    A->>P: INSERT outbox (ShiftClosed)
    P-->>A: OK
    A-->>M: Báo cáo ca: doanh thu, số đơn, variance
    Note over M,A: T+0 — chạy được cả khi mất mạng trung tâm hoàn toàn
```

**Khóa ca là bắt buộc (2026-09-23).** `place_sale` đọc ca bằng `FOR SHARE`, còn đóng ca lấy
`FOR UPDATE` **trước** khi cộng tiền. Thiếu khóa này, một đơn đã đọc ca nhưng chưa kịp chèn
dòng `sale` sẽ commit ngay sau lúc đóng ca cộng tiền. Đơn đó nằm trong ca đã đóng, và
`expected_cash` thiếu đúng số tiền của nó. Khóa ngoại `sale → shift` chỉ chặn được từ lúc
dòng `sale` đã chèn, không che được khe hở giữa đọc ca và chèn đơn.
`test_close_waits_for_a_sale_that_is_committing` dừng đúng trong khe đó (đã kiểm đột biến).

**Đây là luồng chứng minh lớp truy vấn A ([B03](business/03-analytical-workload.md))**: mọi
bước đều nằm trong Postgres cửa hàng, không có bước nào phụ thuộc trung tâm.

---

## 4. Đồng bộ lên trung tâm (bổ sung — khía cạnh lỗi)

[03-architecture §4.2](03-architecture.md) đã vẽ đường thành công. Đây là nhánh **lỗi và
retry**, thường bị bỏ qua khi thiết kế:

```mermaid
sequenceDiagram
    participant W as Sync Worker
    participant P as Postgres (cửa hàng)
    participant X as Central API

    loop mỗi 2s (hoặc ngay lập tức nếu lô trước đầy — xả tồn đọng)
        W->>P: SELECT outbox WHERE sent_at IS NULL AND dead_lettered_at IS NULL<br/>AND (next_attempt_at IS NULL OR next_attempt_at <= now())<br/>ORDER BY id LIMIT 200 FOR UPDATE SKIP LOCKED
        P-->>W: lô sự kiện đến hạn (rỗng nếu không có gì)

        alt Lô rỗng
            Note over W: Ngủ tới vòng lặp sau, không gọi trung tâm
        else Có sự kiện
            W->>X: POST /events (lô, kèm traceparent mỗi event)
            alt 200 — trung tâm đã xét TỪNG sự kiện
                X-->>W: {accepted: [...], rejected: [{event_id, reason, retryable}]}
                W->>P: UPDATE outbox SET sent_at=now() WHERE event_id IN (accepted)
                Note over P: trigger: point_ledger_local.synced_at = sent_at
                alt rejected, retryable=false
                    W->>P: dead_lettered_at=now() — ngay, không đốt lượt thử
                else rejected, retryable=true
                    W->>P: attempts+=1, next_attempt_at = now()+backoff(attempts)
                    Note over W: quá SYNC_MAX_ATTEMPTS → dead-letter
                end
            else 401 / 413 / 429 / 503 / 5xx / mạng / timeout
                Note over W: Lỗi của ĐƯỜNG TRUYỀN — KHÔNG đụng tới attempts của sự kiện nào
                W->>W: sleep(Retry-After nếu có, nếu không thì backoff theo số lần lỗi liên tiếp)
            end
        end
    end
```

> ⚠️ **Đính chính 2026-09-23.** Bản đầu của sơ đồ này có nhánh *"Lỗi mạng / timeout →
> `attempts += 1` → quá ngưỡng thì dead-letter"*. Làm đúng theo đó, một cửa hàng mất mạng vài
> phút sẽ tự đẩy dữ liệu **đúng** của mình vào dead-letter — vi phạm thẳng AT-02 và NFR-01.
> `attempts` chỉ được đếm số lần **trung tâm đã xét và từ chối** sự kiện; mất mạng thì lùi cả
> vòng lặp. Test `test_central_unreachable_never_consumes_attempts` giữ quy tắc này (đã kiểm
> bằng đột biến: đưa lỗi cũ trở lại thì test đỏ).

**Điểm hay bỏ sót:** xử lý lô **một phần thành công** — nếu worker coi cả lô là thất bại
khi chỉ 1/200 sự kiện lỗi, 199 sự kiện kia sẽ bị gửi lại (vô hại nhờ idempotency, nhưng lãng
phí và làm chậm hội tụ). Hợp đồng `{accepted, rejected}` ở [13-api-contracts §2](13-api-contracts.md)
tồn tại chính để giải quyết việc này.

---
*Changelog: 2026-09-23 (lần 2) — §3: khóa ca `FOR SHARE`/`FOR UPDATE` giữa chốt đơn và đóng ca.*

*Changelog: 2026-09-23 — §4 viết lại theo hiện thực sync worker: tách lỗi đường truyền khỏi lỗi
của sự kiện (đính chính nhánh sai), thêm `retryable`, `next_attempt_at`, trigger `synced_at`.*

*Changelog: 2026-09-11 — tạo mới, bổ sung 4 luồng còn thiếu so với 03-architecture.*
