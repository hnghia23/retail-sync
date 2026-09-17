# B01 — Mô hình vận hành thực tế

Doc này mô tả **một chuỗi bán lẻ thực sự vận hành như thế nào** — ai làm gì, vào lúc nào,
với nhịp ra sao. Mục đích không phải kể chuyện, mà để **dẫn xuất yêu cầu kỹ thuật từ hành vi
thật** thay vì từ suy luận trên giấy.

---

## 1. Vai trò và nhịp làm việc

| Vai trò | Số lượng | Ở đâu | Dùng hệ thống để làm gì | Tần suất |
|---|---|---|---|---|
| **Thu ngân** | 2–8 / cửa hàng | Quầy | Bán hàng, tra khách, trả hàng | **Liên tục, cả ngày** |
| **Quản lý cửa hàng** | 1 / cửa hàng | Cửa hàng | Chốt ca, duyệt giảm giá, xem doanh thu hôm nay, kiểm tồn | 5–20 lần/ngày |
| **Quản lý khu vực** | 1 / 10–30 cửa hàng | Di chuyển | So sánh các cửa hàng mình phụ trách | 1–2 lần/ngày |
| **Kế toán / vận hành** | 2–5 | Trụ sở | Đối soát tiền, kiểm tra chênh lệch | Hằng ngày, cuối tháng |
| **Mua hàng / ngành hàng** | 3–10 | Trụ sở | Sản phẩm bán chạy/chậm, tồn kho, đặt hàng | Hằng tuần |
| **Marketing** | 2–5 | Trụ sở | Hiệu quả khuyến mãi, phân khúc khách | Theo chiến dịch |
| **Ban giám đốc** | 2–5 | Trụ sở | Tổng quan doanh thu, xu hướng | Hằng tuần / tháng |

**Quan sát quan trọng:** khối lượng truy vấn **không** tập trung ở trụ sở. Nó tập trung ở
**quầy và quản lý cửa hàng** — những người cần câu trả lời **ngay lập tức về dữ liệu hôm nay**.

Đây là điều thiết kế v2 hiện tại **chưa phản ánh** — xem [B03](03-analytical-workload.md).

## 2. Một ngày làm việc của cửa hàng

```
07:30  Quản lý mở cửa hàng
       → Đăng nhập, xem báo cáo hôm qua, kiểm tiền đầu ca
       → Hệ thống kéo master data mới (giá, khuyến mãi hôm nay)

08:00  Ca sáng bắt đầu — thu ngân đăng nhập, nhận quầy
       → Bán hàng liên tục

11:30  ⚡ Cao điểm trưa (~25% doanh thu ngày)
14:00  Đổi ca → CHỐT CA: đối soát tiền mặt, in báo cáo ca
       → Ca chiều nhận quầy

18:00  ⚡ Cao điểm tối (~35% doanh thu ngày)
21:00  Đóng cửa → CHỐT NGÀY
       → Đối soát tiền, kiểm tồn nhanh, gửi báo cáo lên khu vực

22:00  (Tự động) Trung tâm gom dữ liệu ngày → Lake → Warehouse
```

### Hệ quả kỹ thuật rút ra

| Quan sát | Yêu cầu kỹ thuật |
|---|---|
| Hai đỉnh rõ rệt (trưa, tối) chiếm ~60% ngày | Tải đỉnh cao hơn trung bình ~3–4×, không phải 1.5× như ước lượng ban đầu ở [02-scale-capacity](../02-scale-capacity.md) |
| Chốt ca 2–3 lần/ngày/cửa hàng | Cần thực thể **`shift`** — hiện **chưa có** trong [05-data-model](../05-data-model.md) |
| Master data kéo lúc mở cửa | Đồng bộ master data phải xong trước 08:00; nếu lỗi thì cửa hàng bán bằng giá cũ |
| Báo cáo ca phải in ngay | Truy vấn T+0, không thể đi qua pipeline T+1 |
| Đóng cửa 21:00, ETL 22:00 | Cửa sổ ETL hẹp; nếu cửa hàng ở nhiều múi giờ thì phức tạp hơn (v1 giả định một múi giờ) |

## 3. Nhịp theo tuần / tháng

| Chu kỳ | Việc | Ảnh hưởng hệ thống |
|---|---|---|
| **Thứ 2 hằng tuần** | Họp ngành hàng: sản phẩm bán chạy/chậm tuần trước | Truy vấn quét rộng toàn chuỗi — Class C |
| **Thứ 6 hằng tuần** | Chốt đơn đặt hàng nhà cung cấp | Cần tồn kho + tốc độ bán |
| **Cuối tuần** | Doanh thu cao hơn ngày thường ~40% | Sinh dữ liệu mô phỏng phải phản ánh |
| **Đầu tháng** | Đối soát kế toán tháng trước | Dữ liệu tháng trước phải **đóng băng** và nhất quán |
| **Cuối tháng** | Xét lại hạng khách hàng | Job hàng loạt trên toàn bộ khách |
| **Lễ/Tết** | Doanh thu gấp 2–4× | Kiểm chứng giả định tải đỉnh |
| **Theo chiến dịch** | Khuyến mãi 1–4 tuần | Master data đổi giữa chừng — xem [B02 case E12](02-edge-cases.md) |

## 4. Thực tế hạ tầng tại cửa hàng

Đây là phần hay bị bỏ qua khi thiết kế trên giấy, nhưng quyết định nhiều lựa chọn kỹ thuật.

| Thực tế | Hệ quả |
|---|---|
| Máy tại cửa hàng thường là **mini PC / máy cũ**, 4–8 GB RAM | Stack tại cửa hàng phải nhẹ. Củng cố quyết định modular monolith ([ADR-004](../adr/004-modular-monolith.md)) |
| **Thường không có UPS** — mất điện là tắt máy đột ngột | ⚠️ `synchronous_commit` **không được tắt**. Xem [B02 case I01](02-edge-cases.md) |
| Mạng: cáp quang dân dụng hoặc 4G dự phòng | Đứt vài phút–vài giờ là bình thường |
| **Không có nhân viên IT tại cửa hàng** | Mọi thứ phải tự phục hồi. Không có ai chạy lệnh sửa lỗi |
| Máy quét mã vạch = giả lập bàn phím, gõ rất nhanh + phím Enter | UI phải xử lý input tốc độ cao, chống trùng |
| Máy in hóa đơn nhiệt qua USB/mạng | v1 chỉ cần hiển thị HTML |
| Đồng hồ máy có thể sai | ⚠️ Xem [B02 case I03](02-edge-cases.md) |

## 5. Ràng buộc nghiệp vụ thường bị bỏ sót

| # | Ràng buộc | Vì sao quan trọng |
|---|---|---|
| R1 | **Tiền VND không có phần lẻ**, nhưng chiết khấu theo % sinh số lẻ | Cần quy tắc làm tròn thống nhất, nếu không tổng tiền lệch |
| R2 | Hóa đơn **không được sửa** sau khi in | Mọi điều chỉnh phải là giao dịch đối ứng mới |
| R3 | Giá bán là giá **tại thời điểm bán**, không phải giá hiện tại | Đã xử lý: `unit_price` đóng băng trong `sale_line` |
| R4 | Nhân viên nghỉ việc phải **chặn truy cập ngay** | ⚠️ Xung đột với offline-first. Xem [B02 case S01](02-edge-cases.md) |
| R5 | Chiết khấu vượt ngưỡng cần **quản lý duyệt** | Cần trường ghi nhận ai duyệt |
| R6 | Trả hàng thường **không có hóa đơn giấy** | Phải tra được đơn cũ bằng SĐT khách |
| R7 | Một đơn có thể **thanh toán bằng nhiều hình thức** | ⚠️ Schema hiện tại chỉ có **một** `payment_method`. Xem [B02 case P01](02-edge-cases.md) |
| R8 | Điểm tích lũy có thể bị khiếu nại sau nhiều tháng | Đã xử lý: ledger bất biến ([ADR-002](../adr/002-point-ledger.md)) |

---
*Changelog: 2026-09-11 — tạo mới.*
