# B02 — Danh mục tình huống thực tế

Những tình huống **sẽ xảy ra** khi vận hành thật. Mỗi tình huống ghi: hệ thống hiện tại xử
lý được chưa, và nếu chưa thì cần sửa gì.

**Ký hiệu:** ✅ thiết kế hiện tại đã xử lý · ⚠️ **có lỗ hổng, cần sửa** · 🔵 chấp nhận hạn chế ở v1

---

## P — Thanh toán & hóa đơn

| # | Tình huống | Trạng thái | Xử lý |
|---|---|:---:|---|
| **P01** | **Khách trả 500K tiền mặt + 300K thẻ cho một đơn** | ⚠️ | **LỖ HỔNG.** Schema chỉ có `sale.payment_method` đơn lẻ. Cần tách bảng `sale_payment(sale_id, method, amount, ref)`. Thanh toán tách rất phổ biến ở bán lẻ |
| P02 | Khách đưa dư tiền, cần tính tiền thối | ⚠️ | Cần `tendered_amount` và `change_amount`. Không suy ra được từ `total` |
| P03 | Thu ngân bấm nhầm số lượng, cần xóa dòng trước khi chốt | ✅ | Giỏ hàng chưa chốt nằm ở client (Alpine), chưa ghi DB |
| P04 | Hủy cả đơn **sau khi đã chốt và in** | ✅ | Sinh giao dịch đối ứng, `status = VOIDED`, trỏ `voided_by_sale_id`. Không xóa đơn gốc (R2) |
| P05 | Chiết khấu % tạo số lẻ: 3% của 66.000đ = 1.980đ | ⚠️ | Cần **quy tắc làm tròn thống nhất** ghi trong code và test. Đề xuất: làm tròn tới đồng, phân bổ phần lẻ vào dòng cuối để `SUM(line) = total` |
| P06 | Hai chiết khấu chồng nhau: hạng Gold 5% + khuyến mãi 10% | ⚠️ | Cần chốt quy tắc: **cộng gộp** (15%) hay **nhân tầng** (1−0.95×0.90 = 14.5%)? Phải ghi rõ, nếu không mỗi người hiểu một kiểu |
| P07 | Máy in lỗi, cần in lại hóa đơn | 🔵 | v1 hiển thị HTML, in lại = tải lại trang |
| P08 | Mất điện đúng lúc đang chốt đơn | ✅ | Transaction chưa commit → không có đơn. Xem I01 về cấu hình fsync |

## R — Trả hàng & đổi hàng

| # | Tình huống | Trạng thái | Xử lý |
|---|---|:---:|---|
| R01 | Trả nguyên đơn, trong ngày | ✅ | Giao dịch đối ứng, ledger có dòng âm |
| **R02** | **Trả 2 trong 5 sản phẩm của một đơn** | ⚠️ | **LỖ HỔNG.** Cần `sale_line.original_sale_line_id` để biết dòng trả ứng với dòng mua nào. Nếu không sẽ trả quá số đã mua |
| R03 | Trả hàng **không có hóa đơn giấy** | ⚠️ | Cần tra đơn cũ theo SĐT khách + khoảng ngày. Cần index `sale(customer_id, occurred_at)` |
| R04 | Trả hàng mua ở **cửa hàng khác** | ⚠️ | Cửa hàng B không có đơn của cửa hàng A trong LocalDB → phải hỏi trung tâm → **không làm được khi offline**. Đề xuất v1: chỉ cho trả tại cửa hàng đã mua; v2 mở rộng |
| R05 | Trả hàng sau khi khách đã **lên hạng** nhờ chính đơn đó | ⚠️ | Trừ điểm có thể làm tụt hạng. Chốt: **không tụt hạng trong kỳ**, xét lại vào cuối tháng |
| R06 | Đổi hàng (trả cái này, lấy cái khác) | 🔵 | v1: làm hai giao dịch riêng (trả + bán mới). Đủ dùng |
| R07 | Trả hàng đã **hết khuyến mãi** — hoàn theo giá nào? | ✅ | Theo `unit_price` đóng băng trong `sale_line` gốc |
| R08 | Khách trả hàng nhưng đã tiêu hết điểm | 🔵 | Cho phép số dư âm với `reason = 'RETURN'`, chặn tích lũy mới cho tới khi bù. Hoặc ghi nợ — cần chốt nghiệp vụ |

## C — Khách hàng & định danh

| # | Tình huống | Trạng thái | Xử lý |
|---|---|:---:|---|
| C01 | Khách mua lần đầu tại cửa hàng này, đã có điểm ở chuỗi | ✅ | Cache miss → LocalDB miss → gọi trung tâm |
| C02 | Khách mới hoàn toàn, đăng ký tại quầy **khi đang offline** | ✅ | Tạo cục bộ, `created_locally = true`, đồng bộ sau |
| **C03** | **Hai cửa hàng cùng đăng ký một SĐT khi cả hai offline** | ⚠️ | Sinh hai `customer_id` khác nhau cho cùng người. Trung tâm phải phát hiện trùng `phone_hash` và **gộp**. Ledger làm việc gộp dễ (đổi `customer_id` các dòng) nhưng **cần job phát hiện** |
| C04 | Khách đổi số điện thoại | ⚠️ | `customer_id` là UUID nội bộ nên đổi được — nhưng cần luồng nghiệp vụ và xác thực |
| C05 | Khách quên mang điện thoại, đọc số của người khác | 🔵 | Chấp nhận. Không có cách chống ở POC |
| C06 | Khách không muốn cung cấp SĐT | ✅ | `customer_id = NULL`, bán bình thường |
| C07 | SĐT đã đăng ký cho người khác (số tái sử dụng) | 🔵 | Ghi nhận rủi ro, không xử lý ở v1 |
| C08 | Khách hỏi "sao tôi mất điểm?" | ✅ | Ledger bất biến trả lời được đầy đủ |

## S — Nhân viên & bảo mật

| # | Tình huống | Trạng thái | Xử lý |
|---|---|:---:|---|
| **S01** | **Nhân viên bị sa thải, phải chặn truy cập NGAY — nhưng cửa hàng đang offline** | ⚠️ | **Xung đột nền tảng với offline-first.** JWT không thu hồi được khi cửa hàng không kết nối. Giảm thiểu: access token **ngắn (15 phút)** + danh sách thu hồi kéo cùng master data + quản lý cửa hàng có quyền khóa tài khoản cục bộ ngay lập tức |
| S02 | Thu ngân dùng tài khoản người khác | 🔵 | Ghi nhật ký; không chống được ở POC |
| S03 | Quản lý duyệt chiết khấu vượt ngưỡng | ⚠️ | Cần `sale.authorized_by_employee_id` + ngưỡng cấu hình được |
| S04 | Nhân viên chuyển sang cửa hàng khác | ⚠️ | `employee.store_id` đổi; token cũ vẫn mang `store_id` cũ tới khi hết hạn |
| S05 | Ai đó xem thông tin khách hàng hàng loạt | ⚠️ | Cần nhật ký truy cập PII + cảnh báo bất thường (NFR-06) |

## I — Hạ tầng & môi trường

| # | Tình huống | Trạng thái | Xử lý |
|---|---|:---:|---|
| **I01** | **Mất điện đột ngột (không UPS) giữa lúc ghi** | ⚠️ | **Bắt buộc giữ `synchronous_commit = on`** ở DB cửa hàng. Nhiều hướng dẫn tối ưu khuyên tắt để tăng tốc — **không được làm ở đây**. Mất một giao dịch = mất tiền thật |
| I02 | Ổ cứng cửa hàng đầy | ⚠️ | Cần cảnh báo dung lượng + job dọn `outbox` và log |
| **I03** | **Đồng hồ máy cửa hàng sai 2 giờ** | ⚠️ | `occurred_at` sai → báo cáo sai, phân vùng sai. Cần: NTP bắt buộc + trung tâm **kiểm tra lệch** giữa `occurred_at` và `recorded_at`, cảnh báo nếu > 5 phút |
| I04 | Mạng chập chờn (không đứt hẳn) — timeout liên tục | ⚠️ | Cần **circuit breaker**: sau N lỗi liên tiếp thì ngừng thử trong M giây, tránh mỗi đơn hàng đều chờ 500ms vô ích |
| I05 | Mạng phục hồi, 3 ngày dữ liệu của 50 cửa hàng dồn về | ✅ | Outbox theo lô + backoff có jitter + giới hạn tốc độ ([02 §3.4](../02-scale-capacity.md)) |
| I06 | Máy cửa hàng hỏng, thay máy mới | ⚠️ | Cần quy trình khôi phục: cài lại, kéo master data, **dữ liệu chưa đồng bộ trong outbox thì mất** → cần backup cục bộ hằng ngày |
| I07 | Hai quầy cùng bán một sản phẩm cuối cùng trong kho | ✅ | Cùng một Postgres cửa hàng → transaction xử lý |
| I08 | Máy quét mã vạch gửi ký tự quá nhanh, trùng dòng | ⚠️ | UI cần chống trùng (debounce) và khóa idempotency ở tầng client |

## E — Master data & khuyến mãi

| # | Tình huống | Trạng thái | Xử lý |
|---|---|:---:|---|
| E01 | Giá sản phẩm đổi ở trung tâm lúc 10:00 | ✅ | Cửa hàng kéo về; đơn trước đó giữ giá cũ trong `sale_line` |
| **E12** | **Khuyến mãi hết hạn lúc 12:00, đơn bắt đầu 11:58 chốt lúc 12:01** | ⚠️ | Cần chốt: áp theo **thời điểm mở đơn** hay **thời điểm chốt**? Đề xuất: **thời điểm mở đơn**, ghi `promotion_id` vào đơn ngay khi áp |
| E03 | Cửa hàng offline 2 ngày, giá đã đổi ở trung tâm | 🔵 | Bán theo giá cũ. Chấp nhận được — nhưng cần cảnh báo khi `synced_at` quá cũ |
| E04 | Sản phẩm ngừng kinh doanh nhưng cửa hàng còn tồn | ⚠️ | `is_active = false` không được chặn bán hàng tồn. Cần tách `is_sellable` và `is_orderable` |
| E05 | Sản phẩm mới chưa kịp đồng bộ về cửa hàng | ⚠️ | Thu ngân quét mã không có trong `product_cache` → cần luồng "bán tạm bằng mã thủ công" hoặc chặn |
| E06 | Cửa hàng mới khai trương | ✅ | Đăng ký trong `store` ở trung tâm, kéo master data |
| E07 | Cửa hàng đóng cửa | ⚠️ | Dữ liệu lịch sử phải giữ. `dim_store` cần SCD để báo cáo quá khứ đúng |

## L — Điểm & hạng

| # | Tình huống | Trạng thái | Xử lý |
|---|---|:---:|---|
| L01 | Khách mua ở 2 cửa hàng gần như cùng lúc | ✅ | Ledger — cộng giao hoán ([ADR-002](../adr/002-point-ledger.md)) |
| L02 | Sự kiện đồng bộ gửi lặp 5 lần | ✅ | `event_id` + `ON CONFLICT DO NOTHING` |
| L03 | Khách lên hạng giữa lúc đang mua | ✅ | Hạng đọc lúc mở đơn, không đổi giữa chừng (FR-L05) |
| L04 | Khách tiêu điểm → không được tụt hạng | ✅ | Xếp hạng theo `lifetime_earned` |
| L05 | Quy tắc tích điểm đổi giữa tháng | ⚠️ | Cần `earn_rule` có `valid_from`/`valid_to` và đơn áp quy tắc tại thời điểm bán. Đã có trong schema, cần đảm bảo logic dùng đúng |
| L06 | Điểm hết hạn sau 12 tháng | 🔵 | v2. Ledger đã sẵn sàng (dòng âm `reason = 'EXPIRE'`) |
| L07 | Số dư hiển thị bị tụt khi đồng bộ chậm | ✅ | Số dư hiển thị = trung tâm + ledger cục bộ chưa gửi |

## Z — Chốt ca & đối soát

| # | Tình huống | Trạng thái | Xử lý |
|---|---|:---:|---|
| **Z01** | **Chốt ca: đối chiếu tiền mặt thực đếm vs hệ thống** | ⚠️ | **LỖ HỔNG.** Cần thực thể `shift(id, store_id, employee_id, opened_at, closed_at, opening_cash, counted_cash, expected_cash, variance)` — hiện chưa có trong data model |
| Z02 | Tiền mặt thiếu 50.000đ so với hệ thống | ⚠️ | Cần ghi nhận `variance` + lý do, không được sửa giao dịch |
| Z03 | Ca kéo dài qua nửa đêm | ⚠️ | "Ngày kinh doanh" ≠ ngày lịch. Cần `business_date` riêng trong fact |
| Z04 | Quản lý cần báo cáo ca **ngay lập tức** | ⚠️ | **T+0, không thể qua warehouse.** Xem [B03](03-analytical-workload.md) |

---

## Tổng hợp lỗ hổng phát hiện được

Đây là kết quả có giá trị nhất của doc này — **8 lỗ hổng schema** và **6 quy tắc nghiệp vụ
chưa chốt** mà thiết kế hiện tại bỏ sót.

### Lỗ hổng schema — phải sửa [05-data-model](../05-data-model.md)

| # | Thiếu | Từ case | Mức độ |
|---|---|---|---|
| G1 | Bảng **`sale_payment`** (một đơn, nhiều hình thức thanh toán) | P01 | 🔴 Cao — rất phổ biến |
| G2 | **`tendered_amount`, `change_amount`** (tiền khách đưa, tiền thối) | P02 | 🟡 Vừa |
| G3 | **`sale_line.original_sale_line_id`** (trả hàng một phần) | R02 | 🔴 Cao |
| G4 | Thực thể **`shift`** (chốt ca, đối soát tiền) | Z01, Z02 | 🔴 Cao — nghiệp vụ hằng ngày |
| G5 | **`business_date`** tách khỏi ngày lịch | Z03 | 🟡 Vừa |
| G6 | **`sale.authorized_by_employee_id`** (duyệt chiết khấu) | S03 | 🟡 Vừa |
| G7 | Tách **`is_sellable`** / **`is_orderable`** cho sản phẩm | E04 | 🟢 Thấp |
| G8 | **`sale.promotion_id`** ghi nhận khuyến mãi đã áp | E12 | 🟡 Vừa |

### Quy tắc nghiệp vụ chưa chốt — cần quyết định

| # | Câu hỏi | Đề xuất |
|---|---|---|
| Q-B1 | Chiết khấu hạng + khuyến mãi: cộng gộp hay nhân tầng? | **Cộng gộp**, đơn giản và dễ giải thích cho khách |
| Q-B2 | Làm tròn VND khi chiết khấu lẻ | Làm tròn tới đồng; phần lẻ dồn vào dòng cuối để `SUM(line) = total` |
| Q-B3 | Khuyến mãi áp theo thời điểm mở đơn hay chốt đơn? | **Mở đơn** |
| Q-B4 | Trả hàng ở cửa hàng khác nơi mua? | **Không ở v1** (cần online) |
| Q-B5 | Trả hàng có làm tụt hạng không? | **Không trong kỳ**; xét lại cuối tháng |
| Q-B6 | Cho phép số dư điểm âm khi trả hàng? | **Có**, chặn tích lũy mới tới khi bù |

### Ràng buộc hạ tầng bắt buộc

| # | Ràng buộc | Từ case |
|---|---|---|
| I-1 | **`synchronous_commit = on`** ở Postgres cửa hàng — không được tắt để tối ưu | I01 |
| I-2 | NTP bắt buộc + trung tâm cảnh báo lệch `occurred_at` vs `recorded_at` > 5 phút | I03 |
| I-3 | Circuit breaker cho mọi lời gọi tới trung tâm | I04 |
| I-4 | Backup cục bộ hằng ngày ở cửa hàng (outbox chưa gửi là dữ liệu chưa có ở đâu khác) | I06 |
| I-5 | Job phát hiện khách trùng `phone_hash` ở trung tâm | C03 |

---
*Changelog: 2026-09-11 — tạo mới.*
