# B04 — Hệ quả lên Tech Stack

Tổng hợp những gì [B01](01-operating-model.md), [B02](02-edge-cases.md), [B03](03-analytical-workload.md)
buộc phải thay đổi trong stack đã chốt ở [07-stack-decision](../07-stack-decision.md).

> **Tóm tắt:** stack đứng vững, **không đổi thành phần nào**. Bài tập này phát hiện một lỗ
> hổng kiến trúc thật (thiếu lớp báo cáo vận hành T+0) và 8 lỗ hổng schema.
>
> **Trạng thái sau khi chủ dự án quyết (2026-09-11):**
> - ClickHouse: ✅ **giữ, mức Must** — ưu tiên là scale, nên chấp nhận chi phí sớm để tránh
>   di trú ở ~60–100 cửa hàng
> - Quy tắc nghiệp vụ: ✅ **giải bằng cấu hình**, không hardcode (§6)
> - Ưu tiên số 1 là **ổn định + scale** → xem [08-reliability-and-scale.md](../08-reliability-and-scale.md)

---

## 1. Thay đổi bắt buộc: bổ sung lớp báo cáo vận hành 🔴

**Phát hiện:** lớp truy vấn có tần suất cao nhất (chốt ca, doanh thu hôm nay, tra hóa đơn cũ)
**chưa được thiết kế**. Toàn bộ tuần 3 phục vụ lớp có tần suất thấp nhất.

**Thay đổi:**

```
MỚI   Lớp A — Báo cáo vận hành  →  đọc thẳng Postgres CỬA HÀNG (T+0, chạy khi offline)
MỚI   Lớp B — Báo cáo quản lý   →  đọc Postgres TRUNG TÂM       (T+0 cho hôm nay)
CŨ    Lớp C — Phân tích chuỗi   →  Lake → Warehouse              (T+1)
```

**Chi phí:** ~1 ngày. Chỉ là vài truy vấn SQL có index + vài trang HTMX. Không thêm hạ tầng.

**Đưa vào lộ trình:** tuần 2 (cùng lúc với chốt ca), **ưu tiên Must**.

**Vì sao bắt buộc, không phải "nên có":** chốt ca là nghiệp vụ **hằng ngày, bắt buộc** của
mọi cửa hàng bán lẻ. Không có nó thì hệ thống không dùng được thật, bất kể warehouse đẹp đến
đâu.

---

## 2. ClickHouse — ✅ **GIỮ** *(chốt 2026-09-11)*

> **Quyết định của chủ dự án:** giữ ClickHouse, vì ưu tiên số 1 là **ổn định + khả năng scale**.
>
> **Câu trả lời thẳng cho câu hỏi "có phù hợp hơn Postgres không":** Có — làm *warehouse* thì
> ClickHouse rõ ràng phù hợp hơn, đó là việc nó sinh ra để làm. Phân tích dưới đây chỉ ra rằng
> ở *quy mô POC* Postgres cũng đủ — nhưng đó là lập luận về **thời điểm cần**, không phải về
> **engine nào đúng**. Khi ưu tiên là scale, ClickHouse tránh được một cuộc di trú đã biết
> trước ở ~60–100 cửa hàng.
>
> **Phương án chốt: A (giữ nguyên)** thay vì C như tôi khuyến nghị ban đầu. ClickHouse quay
> lại mức **Must**. Phương án dự phòng (DuckDB trên lake) vẫn ghi lại ở đây phòng khi tuần 3
> thực sự cháy.

<details>
<summary>Phân tích gốc (giữ lại để tham chiếu)</summary>

### Xem lại ClickHouse

### Số liệu

Ở **T0 (quy mô POC)**: toàn bộ dữ liệu ~**767 nghìn dòng**. Ở **T1 (20 cửa hàng)**: ~**15 triệu
dòng** cho 24 tháng.

Ngưỡng Postgres: ~12 triệu dòng cho dashboard 3 giây, ~40 triệu cho báo cáo 10 giây.

> **Ở quy mô POC, ClickHouse không phục vụ một truy vấn nào mà Postgres không làm được.**
> Nó chỉ thật sự cần từ khoảng **60–100 cửa hàng**.

Hai ngoại lệ Postgres không cứu được: **market basket analysis** (self-join, vượt tầm từ T1)
và **phân tích tự do trên dữ liệu thô** (nhu cầu tổ chức ở T2+).

### Ba phương án

| | **A. Giữ nguyên** | **B. Hoãn ClickHouse** | **C. Giữ nhưng hạ ưu tiên** ⭐ |
|---|---|---|---|
| Warehouse v1 | ClickHouse | **PostgreSQL** | ClickHouse |
| Lake | MinIO + Parquet | MinIO + Parquet | MinIO + Parquet |
| Tuần 3 | 4 công nghệ mới trong 5 ngày | 2 công nghệ mới | 4 công nghệ, nhưng cắt được |
| RAM | ~7 GB | ~6 GB | ~7 GB |
| Rủi ro lịch | 🔴 Cao | 🟢 Thấp | 🟡 Vừa |
| Chi phí về sau | 0 | ~1–2 ngày port dbt model sang CH | 0 |
| Thể hiện kiến trúc scale | ✅ Đầy đủ | 🟡 Chỉ có lake | ✅ Đầy đủ |

**Khuyến nghị: C.** Giữ ClickHouse trong kiến trúc (spec của bạn có nêu Lake → Warehouse, và
nó là phần thể hiện đường scale), nhưng:

- **Must:** lake bronze + lớp báo cáo vận hành + rollup cơ bản
- **Should:** star schema đầy đủ trên ClickHouse
- **Nếu tuần 3 trượt:** cắt ClickHouse, dùng **DuckDB truy vấn thẳng Parquet trong lake** để
  demo phân tích — không tốn hạ tầng nào, và lake vẫn còn nguyên làm nền cho ClickHouse sau

Phương án C giữ được mọi thứ mà không đặt cược cả tuần 3 vào việc học 4 công cụ cùng lúc.

**Lưu ý về phương án B:** nếu chọn hoãn, chi phí port dbt sang ClickHouse sau này là ~1–2 ngày
cho ~15 model (khác biệt chủ yếu ở cấu hình materialization: `ENGINE`, `ORDER BY`,
`PARTITION BY`, và chiến lược incremental). Không lớn, nhưng có thật.

</details>

---

## 3. Không thay đổi — những quyết định được củng cố ✅

Bài tập này **xác nhận** phần lớn stack:

| Quyết định | Bằng chứng củng cố từ nghiệp vụ |
|---|---|
| **Point ledger** ([ADR-002](../adr/002-point-ledger.md)) | Case L01 (mua 2 nơi), R05 (trả hàng ảnh hưởng hạng), C08 (khiếu nại điểm), C03 (gộp khách trùng) — ledger xử lý cả bốn mà không cần thêm gì |
| **Offline-first** ([NFR-01](../01-requirements.md)) | Case Z04 — chốt ca là nghiệp vụ bắt buộc hằng ngày và phải chạy khi mất mạng |
| **Postgres ở cửa hàng** ([ADR-008 C0](../adr/008-remaining-decisions.md)) | Lớp A và B đều chạy trên DB cửa hàng → cần engine có index tốt và gộp nhóm được, không chỉ là kho ghi |
| **Outbox** ([ADR-003](../adr/003-outbox-not-kafka.md)) | Case I05 — 3 ngày dữ liệu dồn về khi mạng phục hồi |
| **`unit_price` đóng băng** | Case R07, E01 — trả hàng và đổi giá |
| **Modular monolith** ([ADR-004](../adr/004-modular-monolith.md)) | B01 §4 — máy cửa hàng 4–8 GB RAM, không có IT tại chỗ |
| **Airflow** ([ADR-007](../adr/007-airflow-over-dagster.md)) | Không đổi — chỉ phục vụ lớp C |

**Một điểm cần chỉnh:** [02-scale-capacity](../02-scale-capacity.md) giả định 15% lượng ngày
dồn vào giờ cao điểm. B01 §2 cho thấy thực tế có **hai đỉnh** (trưa ~25%, tối ~35%) → tải đỉnh
cao hơn ước lượng **~2×**. Vẫn không đổi kết luận (T3 đỉnh ~130 w/s thay vì ~67), Postgres vẫn
dư 40–75×.

---

## 4. Lỗ hổng schema — phải sửa [05-data-model](../05-data-model.md) 🔴

Từ [B02](02-edge-cases.md). **Bốn cái mức Cao phải có ở v1:**

```sql
-- G1: Thanh toán tách (P01) — MỘT đơn, NHIỀU hình thức
CREATE TABLE sale_payment (
    sale_id uuid, seq int,
    method text NOT NULL,          -- CASH | CARD | EWALLET
    amount bigint NOT NULL CHECK (amount > 0),
    reference text,                -- mã giao dịch thẻ/ví
    PRIMARY KEY (sale_id, seq)
);
-- Bỏ cột sale.payment_method. Bất biến: SUM(sale_payment.amount) = sale.total

-- G2: Tiền khách đưa / tiền thối (P02)
ALTER TABLE sale ADD COLUMN tendered_amount bigint;
ALTER TABLE sale ADD COLUMN change_amount   bigint;

-- G3: Trả hàng một phần (R02)
ALTER TABLE sale_line ADD COLUMN original_sale_id      uuid;
ALTER TABLE sale_line ADD COLUMN original_sale_line_no int;
-- Bất biến: SUM(số lượng trả) <= số lượng đã mua của dòng gốc

-- G4: Ca làm việc (Z01, Z02) — nghiệp vụ hằng ngày, hiện chưa có
CREATE TABLE shift (
    shift_id uuid PRIMARY KEY,
    store_id text NOT NULL,
    opened_by_employee_id text NOT NULL,
    closed_by_employee_id text,
    opened_at timestamptz NOT NULL,
    closed_at timestamptz,
    business_date date NOT NULL,        -- G5: ngày kinh doanh ≠ ngày lịch (Z03)
    opening_cash  bigint NOT NULL,
    expected_cash bigint,               -- hệ thống tính
    counted_cash  bigint,               -- thực đếm
    variance      bigint,               -- chênh lệch, KHÔNG sửa giao dịch để bù
    variance_note text
);
ALTER TABLE sale ADD COLUMN shift_id uuid REFERENCES shift(shift_id);
```

**Bốn cái mức Vừa/Thấp:** `sale.authorized_by_employee_id` (S03), `sale.promotion_id` (E12),
tách `is_sellable`/`is_orderable` (E04), `business_date` trong fact (Z03).

**Ảnh hưởng warehouse:** `fact_sale_line` cần thêm `shift_key` và `business_date`; cần
`fact_payment` riêng vì một đơn giờ có nhiều dòng thanh toán (nếu không sẽ nhân đôi doanh thu
khi join).

---

## 5. Ràng buộc hạ tầng mới 🔴

Từ [B02](02-edge-cases.md) §I — những thứ không lộ ra khi thiết kế trên giấy:

| # | Ràng buộc | Vì sao |
|---|---|---|
| **I-1** | **`synchronous_commit = on`** ở Postgres cửa hàng — **không được tắt để tối ưu** | Cửa hàng thường không có UPS. Mất một giao dịch = mất tiền thật. Rất nhiều hướng dẫn tối ưu Postgres khuyên tắt — ở đây thì không |
| I-2 | NTP bắt buộc; trung tâm cảnh báo khi lệch `occurred_at` vs `recorded_at` > 5 phút | Đồng hồ cửa hàng sai làm hỏng báo cáo và phân vùng |
| I-3 | **Circuit breaker** cho mọi lời gọi tới trung tâm | Mạng chập chờn: nếu không có, mỗi đơn đều chờ timeout 500ms vô ích |
| I-4 | Backup cục bộ hằng ngày ở cửa hàng | Outbox chưa gửi là dữ liệu **chưa tồn tại ở đâu khác** |
| I-5 | Job phát hiện khách trùng `phone_hash` ở trung tâm | Hai cửa hàng cùng offline có thể tạo hai `customer_id` cho một người |

---

## 6. Sáu quy tắc nghiệp vụ — ✅ **giải bằng cấu hình, không hardcode**

> **Chỉ đạo của chủ dự án (2026-09-11):** các quy tắc này đặt ra để có cái nhìn tổng quan,
> **không quá quan trọng** — khi tích hợp vào code có thể tùy chỉnh theo tình hình thực tế.

**Cách áp dụng đúng chỉ đạo này về mặt kỹ thuật:** không hardcode quy tắc nào. Mỗi quy tắc là
**một tham số cấu hình hoặc một strategy object**, mặc định theo cột "Đề xuất", đổi được mà
không phải sửa logic:

```python
class PricingRules(BaseModel):
    discount_stacking: Literal["additive", "multiplicative"] = "additive"  # Q-B1
    rounding: Literal["to_dong_last_line", "per_line"] = "to_dong_last_line"  # Q-B2
    promotion_applies_at: Literal["order_open", "order_close"] = "order_open"  # Q-B3


class ReturnRules(BaseModel):
    allow_cross_store: bool = False  # Q-B4
    demote_tier_on_return: bool = False  # Q-B5
    allow_negative_balance: bool = True  # Q-B6
```

**Hai quy tắc có hệ quả cấu trúc**, không chỉ là logic — cần *một* quyết định từ ngày đầu dù
sau này đổi giá trị:

| | Vì sao có hệ quả cấu trúc |
|---|---|
| **Q-B2** (làm tròn) | Quyết định bất biến `subtotal = total + discount` có giữ được không. Đây là `CHECK` constraint — làm tròn sai thì **giao dịch bị từ chối**, không phải chỉ lệch số |
| **Q-B6** (số dư âm) | Quyết định có `CHECK (balance >= 0)` trên `point_balance` hay không. Thêm/bỏ constraint sau là migration |

Bốn quy tắc còn lại thuần logic, đổi lúc nào cũng được.

| # | Câu hỏi | Đề xuất |
|---|---|---|
| Q-B1 | Chiết khấu hạng + khuyến mãi: **cộng gộp** hay **nhân tầng**? | Cộng gộp — dễ giải thích cho khách |
| Q-B2 | Làm tròn VND khi chiết khấu ra số lẻ | Làm tròn tới đồng; phần lẻ dồn vào dòng cuối để `SUM(line) = total` |
| Q-B3 | Khuyến mãi áp theo thời điểm **mở đơn** hay **chốt đơn**? | Mở đơn |
| Q-B4 | Cho trả hàng ở cửa hàng khác nơi mua? | Không ở v1 (cần online) |
| Q-B5 | Trả hàng có làm **tụt hạng** không? | Không trong kỳ; xét lại cuối tháng |
| Q-B6 | Cho phép số dư điểm **âm** khi trả hàng? | Có, chặn tích lũy mới tới khi bù |

---

## 7. Tổng hợp thay đổi lộ trình

| Thay đổi | Ảnh hưởng [06-roadmap](../06-roadmap.md) |
|---|---|
| 🔴 Thêm lớp báo cáo vận hành | **Tuần 2, Must.** +1 ngày |
| 🔴 Schema: `sale_payment`, `shift`, trả hàng một phần | **Tuần 1 ngày 2** (migration). +0.5 ngày |
| 🟡 ClickHouse hạ xuống Should | Tuần 3 giảm rủi ro; có phương án cắt (DuckDB trên lake) |
| 🔴 `synchronous_commit`, circuit breaker, NTP | Tuần 1–2, rải rác. +0.5 ngày |
| 🟡 Chốt 6 quy tắc nghiệp vụ | Trước tuần 1 — không tốn thời gian code |
| 🟢 Tải đỉnh ×2 so với ước lượng | Chỉ cập nhật doc, không đổi kiến trúc |

**Ròng: +2 ngày việc Must.** Bù lại bằng việc hạ ClickHouse xuống Should (giải phóng ~2 ngày
tuần 3) và đã bỏ redeem điểm ([ADR-008 C4](../adr/008-remaining-decisions.md), ~1 ngày).

Lộ trình 4 tuần vẫn khả thi, nhưng **đệm đã mỏng hơn**.

---
*Changelog: 2026-09-11 — tạo mới.*
