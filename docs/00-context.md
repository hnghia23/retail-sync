# 00 — Bối cảnh & Ràng buộc

## Bài toán

Chuỗi cửa hàng bán lẻ cần một hệ thống dữ liệu gồm hai năng lực:

1. **POS** — ghi nhận đơn hàng và khách hàng tại từng cửa hàng
2. **Loyalty** — tích điểm, xếp hạng và ưu đãi cho khách hàng xuyên suốt toàn chuỗi

Cộng thêm một tầng phân tích trung tâm để trả lời câu hỏi kinh doanh (doanh thu, sản phẩm,
hành vi khách, hiệu quả loyalty).

## Vì sao bài toán này không tầm thường

Nếu chỉ có một cửa hàng, đây là một ứng dụng CRUD. Cái làm nó khó là **tính phân tán**:

- Cửa hàng ở nhiều nơi, đường truyền không đảm bảo. **Mất mạng không được phép ngừng bán hàng.**
- Một khách hàng mua ở nhiều cửa hàng. Điểm của họ được cộng từ nhiều nơi, có thể **đồng thời**.
- Điểm tích lũy là **tiền** — khách sẽ khiếu nại nếu sai. Phải kiểm toán được.
- Dữ liệu giao dịch phải hội tụ về trung tâm để phân tích, **không được trùng, không được mất**.

Phần lớn độ phức tạp nằm ở đây, **không nằm ở lưu lượng** (xem [02-scale-capacity.md](02-scale-capacity.md)).

## Ràng buộc dự án

| Yếu tố | Giá trị | Hệ quả thiết kế |
|---|---|---|
| **Mục đích** | POC cho doanh nghiệp thật | Phải đúng về nguyên lý, có đường lên production. Không được là đồ chơi. |
| **Quy mô mục tiêu** | Chưa xác định — thiết kế để lớn dần | Bắt đầu đơn giản, nhưng mọi lựa chọn phải có **scale seam** ghi rõ. |
| **Môi trường** | Docker trên laptop cá nhân | Tổng RAM stack phải vừa một máy (~16GB). Loại bỏ thành phần nặng. |
| **Nhân lực** | 1 người | Không microservices. Không stack 8 hệ thống. Ưu tiên công cụ làm hộ nhiều việc. |
| **Thời gian** | ~1 tháng | Phạm vi phải cắt quyết liệt. MoSCoW nghiêm ngặt. |
| **Công cụ** | Có Claude Code hỗ trợ | Nhanh hơn đáng kể ở boilerplate/schema/test. **Không** nhanh hơn ở debug phân tán. |

### Đọc kỹ ràng buộc "1 người, 1 tháng"

Đây là ràng buộc chi phối nhất. Một tháng part-time solo ≈ **80–120 giờ làm việc thật**.

Claude Code tăng tốc mạnh ở: sinh schema, CRUD, test, dbt models, docker config, docs.
Claude Code **không** tăng tốc ở: debug race condition, tinh chỉnh consistency, quyết định
kiến trúc, chạy thử hạ tầng.

→ Ngân sách thực tế: **~4 thành phần hạ tầng**, không phải 8. Mỗi hệ thống thêm vào tốn
khoảng 3–6 giờ chỉ để dựng, nối và gỡ lỗi — chưa tính code nghiệp vụ.

## Phạm vi

### Trong phạm vi v1

- Bán hàng tại quầy: tra sản phẩm → giỏ hàng → thanh toán → lưu đơn
- Nhận diện khách, đăng ký khách mới, tích điểm, xếp hạng, ưu đãi theo hạng
- Trả hàng và hoàn điểm
- Hoạt động khi mất kết nối trung tâm
- Đồng bộ hai chiều store ↔ trung tâm
- Lake dữ liệu thô + Warehouse mô hình chiều + dashboard

### Thứ tự thực hiện *(bổ sung 2026-09-23 — [ADR-010](adr/010-data-flow-first.md))*

Phạm vi trên là **sản phẩm cuối**. Trong ngân sách 1 tháng, dự án chỉ làm **luồng dữ liệu**
và **bằng chứng ổn định**. Tính năng dùng tại quầy để sau:

| Giai đoạn | Làm gì | Dữ liệu từ đâu |
|---|---|---|
| **A — Luồng dữ liệu** | Cửa hàng → outbox → trung tâm → bronze → ClickHouse, đúng và đủ ở mọi tầng ([17](17-data-flow.md)) | **Bộ giả lập** ([18](18-simulator.md)), không phải UI |
| **B — Chứng minh** | Hỗn loạn, ngâm 72h, tải, scale seam, tất cả bằng số đo | Bộ giả lập ở quy mô T0 → T3 |
| **C — Tính năng** | Tra khách, báo cáo ca, UI đăng ký/trả hàng/chốt ca, master data, dashboard | Người dùng thật |

Chủ dự án không cần một ứng dụng dùng được ngay. Cái cần là **một luồng dữ liệu đã được chứng
minh chạy ổn định**, để các tính năng xây sau có nền đáng tin.

### Ngoài phạm vi v1 (ghi rõ để khỏi trôi phạm vi)

| Không làm | Lý do |
|---|---|
| Kế toán, thuế, hóa đơn điện tử | Miền riêng, nhiều quy định pháp lý, không phục vụ mục tiêu POC |
| Quản lý kho nâng cao (đặt hàng, chuyển kho, kiểm kê) | v1 chỉ trừ tồn khi bán |
| E-commerce / omnichannel | Mở rộng sau, kiến trúc không chặn |
| App di động cho khách | Không cần để chứng minh kiến trúc |
| Nhân sự, chấm công, lương | **Lưu ý:** `salary` nằm trong schema v1 là sai chỗ — đó là dữ liệu HR nhạy cảm, không thuộc DB của POS |
| Đa tiền tệ, đa quốc gia | Giả định VND, một múi giờ |
| Chống gian lận, phát hiện bất thường | Có dữ liệu rồi mới làm được |

## Giả định (cần kiểm chứng khi có dữ liệu thật)

| # | Giả định | Rủi ro nếu sai |
|---|---|---|
| A1 | ~3.5 dòng sản phẩm mỗi đơn | Ảnh hưởng ước lượng dung lượng, không ảnh hưởng kiến trúc |
| A2 | ~60% giao dịch có nhận diện khách hàng | Nếu cao hơn nhiều → tải lookup trung tâm tăng, nhưng vẫn nhỏ |
| A3 | 15% lượng giao dịch dồn vào giờ cao điểm | Nếu tập trung hơn (VD 30%) → nhân đôi tải đỉnh, vẫn trong khả năng |
| A4 | Một cửa hàng có 1–4 quầy thu ngân | Nếu >10 quầy → cân nhắc lại DB edge |
| A5 | Mất mạng cửa hàng: vài phút đến vài giờ, hiếm khi vài ngày | Nếu mất nhiều ngày → cần xét lại chính sách điểm khi offline |
| A6 | Khách hàng định danh bằng số điện thoại | Là PII → chi phối yêu cầu bảo mật |

## Quan hệ với prototype v1

Code trong `store/`, `central/` là bản thử nghiệm đã dựng được hạ tầng nhưng chưa nối
thông nghiệp vụ. Quyết định: **làm lại từ đầu**, giữ v1 làm tham khảo.

Những thứ **giữ lại** từ v1:
- Dataset đã crawl: `crawl_data/products/` (~400KB sản phẩm thật), `crawl_data/store_info/`
- Ý tưởng star schema cho warehouse (thiết kế đúng hướng)
- Cấu trúc clean architecture ở `pos_service` (domain/use_case/adapter tách bạch tốt)

Những thứ **bỏ**: xem [09-v1-postmortem.md](09-v1-postmortem.md) và [04-tech-stack.md](04-tech-stack.md).

---
*Changelog: 2026-09-23 — thêm "Thứ tự thực hiện" theo ADR-010.*

*Changelog: 2026-09-11 — tạo mới.*
