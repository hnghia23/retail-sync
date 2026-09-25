# Business Requirements & Vận hành thực tế

> **Trạng thái (2026-09-25):** đây là **bản phân tích tại thời điểm 2026-09-11**, giữ nguyên để
> truy được vì sao thiết kế đổi. Các "lỗ hổng" nó chỉ ra đã được hấp thụ vào thiết kế và code:
> thực thể `shift` + mở/đóng ca, `business_date`, `tendered_amount`/`change_amount`,
> `authorized_by_employee_id`, `promotion_id`, lớp truy vấn A/B/C ([03 §5](../03-architecture.md)).
> Lớp truy vấn A (báo cáo tại quầy) là tính năng giai đoạn C theo
> [ADR-010](../adr/010-data-flow-first.md). Tiến độ thật ở [docs/progress/](../progress/).

Bộ tài liệu này trả lời một câu hỏi mà phần thiết kế kỹ thuật không trả lời được:

> **Hệ thống này thực sự được dùng như thế nào, bởi ai, bao nhiêu lần một ngày?**

Mục đích không phải mô tả nghiệp vụ cho đẹp, mà để **dẫn xuất và kiểm tra lại quyết định kỹ
thuật từ hành vi thật** — thay vì suy luận từ kiến trúc trên giấy.

## Đọc theo thứ tự

| # | Tài liệu | Trả lời |
|---|---|---|
| B01 | [01-operating-model.md](01-operating-model.md) | Ai làm gì, nhịp ngày/tuần/tháng, thực tế hạ tầng tại cửa hàng |
| B02 | [02-edge-cases.md](02-edge-cases.md) | ~50 tình huống **sẽ xảy ra** khi vận hành thật — cái nào đã xử lý, cái nào chưa |
| B03 | [03-analytical-workload.md](03-analytical-workload.md) | Truy vấn nào thực sự chạy, khối lượng bao nhiêu, engine nào đúng |
| B04 | [04-stack-implications.md](04-stack-implications.md) | **Những gì phải thay đổi trong stack** |

## Kết quả chính của bài tập này

Ba phát hiện mà phần thiết kế kỹ thuật đã bỏ sót:

### 1. 🔴 Thiếu hẳn một lớp truy vấn — và đó là lớp dùng nhiều nhất

Có **ba lớp** truy vấn khác nhau về bản chất, không phải một:

| Lớp | Độ tươi | Tần suất thực tế | Thiết kế v2 |
|---|---|---|---|
| **A — Vận hành** *(chốt ca, doanh thu hôm nay, tra hóa đơn cũ)* | **T+0 bắt buộc** | **Cao nhất** | ❌ **Không có gì** |
| B — Quản lý cửa hàng | T+0 → T+1 | Trung bình | 🟡 Chỉ có phần T+1 |
| C — Phân tích chuỗi | T+1 | Thấp nhất | ✅ Cả tuần 3 |

Toàn bộ tuần 3 của lộ trình phục vụ lớp **ít dùng nhất**, trong khi lớp **dùng nhiều nhất**
chưa được thiết kế. Lớp A không thể đi qua warehouse T+1 — quản lý cần số của *hôm nay*.

> 🔀 **Cập nhật 2026-09-23 — [ADR-010](../adr/010-data-flow-first.md).** Phát hiện ở trên vẫn
> đúng: lớp A là lớp dùng nhiều nhất và phải đọc thẳng Postgres cửa hàng. Nhưng chủ dự án đã
> chọn **làm luồng dữ liệu và bằng chứng ổn định trước**, nên báo cáo lớp A dời sang giai đoạn C.
> Dời được mà không tốn gì về sau: lớp A chỉ là truy vấn **đọc** trên các bảng và index đã có
> sẵn ở Postgres cửa hàng ([05 §3.6](../05-data-model.md)). Không đổi schema, không đổi luồng.

### 2. 🟡 ClickHouse không cần thiết ở quy mô POC — trực giác của bạn đúng

| | Dòng fact | Postgres |
|---|---:|---|
| T0 (POC) | ~767 K | ✅ Thoải mái |
| T1 (20 CH, 24 tháng) | ~15 Tr | ✅ ~4 giây |
| T2 (200 CH, 24 tháng) | ~256 Tr | ❌ Không kịp |

Ngưỡng Postgres: ~12 triệu dòng cho dashboard 3 giây. **ClickHouse chỉ thật sự cần từ khoảng
60–100 cửa hàng.** Hai ngoại lệ Postgres không cứu được: market basket analysis (self-join) và
phân tích tự do trên dữ liệu thô.

→ Khuyến nghị: giữ ClickHouse nhưng **hạ xuống Should**, có phương án cắt (DuckDB trên lake).

### 3. 🔴 Tám lỗ hổng schema

Bốn cái mức Cao, phát hiện từ tình huống thực tế:

| Thiếu | Từ tình huống |
|---|---|
| Bảng `sale_payment` | Khách trả 500K tiền mặt + 300K thẻ cho một đơn |
| Thực thể `shift` | Chốt ca, đối soát tiền — nghiệp vụ **hằng ngày** |
| `sale_line.original_sale_line_id` | Trả 2 trong 5 sản phẩm |
| `tendered_amount` / `change_amount` | Tính tiền thối |

Cộng với **5 ràng buộc hạ tầng** mới (quan trọng nhất: **`synchronous_commit = on`** vì cửa
hàng không có UPS) và **6 quy tắc nghiệp vụ** cần chốt.

## Quan hệ với phần thiết kế kỹ thuật

```
docs/business/  ──(dẫn xuất yêu cầu)──►  docs/01-requirements.md
       │                                  docs/05-data-model.md
       └────────(kiểm tra lại)──────────►  docs/07-stack-decision.md
                                           docs/adr/*.md
```

Khi hai bên mâu thuẫn: **docs/business mô tả *cái đang có thật*, docs kỹ thuật mô tả *cái sẽ
xây*.** Nghiệp vụ thắng — nếu thiết kế không phục vụ được tình huống thật thì thiết kế sai.

---
*Changelog: 2026-09-11 — tạo mới.*
