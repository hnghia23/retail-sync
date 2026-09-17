# 99 — Spec gốc (nguyên văn)

> Đây là bản mô tả yêu cầu **nguyên văn do user viết ban đầu**, giữ lại nguyên trạng để
> tham chiếu. Thiết kế v2 đã diễn giải và mở rộng nó thành
> [01-requirements.md](01-requirements.md) và [05-data-model.md](05-data-model.md) —
> khi hai bên khác nhau, doc 01 và 05 là bản có hiệu lực.
>
> Những chỗ v2 **cố tình làm khác** so với spec này:
> - `Cassandra` cho central loyalty DB → đổi sang PostgreSQL ([ADR-001](adr/001-postgres-everywhere.md))
> - `MySQL` cho POS → đổi sang PostgreSQL ([ADR-001](adr/001-postgres-everywhere.md))
> - `point` dạng số dư → đổi sang ledger append-only ([ADR-002](adr/002-point-ledger.md))
> - `employee.salary` → loại bỏ (dữ liệu HR, không thuộc hệ thống này)
> - Bổ sung `store_id` vào mọi bảng giao dịch (spec gốc thiếu)
> - Bổ sung `region`, `warehouse`, `promotion` (spec có nêu trong Dataset nhưng chưa có bảng)

---

Retail: chuỗi cửa hàng

Yêu cầu:

- lưu trữ thông tin đơn hàng, khách hàng - POS
- tích điểm cho khách hàng - Loyalty

Workflow:

POS:

- nhận đơn hàng
- lấy thông tin khách hàng từ Loyalty (kiểm tra cache, nếu không có call API để đồng bộ thông tin từ CentralDB)
- lưu trữ thông tin đơn hàng (POS LocalDB)
- Đồng bộ vào Lake >> Warehouse

Loyalty:

- Tương tác với POS extract thông tin khách hàng
- LocalDB lưu thông tin khách hàng đã từng mua tại cửa hàng
- Đồng bộ thông tin khách hàng mới từ CentralDB
- CentralDB >> Lake >> Warehouse (nếu cần phân tích)

Architecture

!IMG_2716.jpg

Mô tả chi tiết luồng hoạt động hệ thống:

POS:

- nhân viên đăng nhập bằng tài khoản vào hệ thống
- nhập mã sản phẩm + số lượng
- gửi yêu cầu đến pos để lấy thông tin giá cho từng sản phẩm
- hệ thống pos trả về đơn giá truy xuất từ db (MySQL)
- pos trả về hóa đơn trên ui
- lưu thông tin đơn hàng vào db

Loyalty:

- nếu khách hàng cung cấp id tích điểm:
    - pos gửi thông tin id về hệ thống loyalty
    - kiểm tra xem có tồn tại trong cache (Redis)
    - nếu không tồn tại, kiểm tra có tồn tại trong db local (PostgreSQL) không
    - nếu không tồn tại (khách mua hàng lần đầu), gửi request về hệ thống central db (Cassandra) của loyalty để lấy thông tin tích điểm của khách hàng thông qua id
    - cộng thêm điểm tích lũy theo id
    - đồng bộ hệ thống

Central:

- Connect đến hệ thống POS của từng cửa hàng
- Lấy data định kì về Lake (MinIO)
- Thu thập Loyalty về Lake
- Đưa data về Data Warehouse

Data:  

POS:

- store: id, name, address, city, manager_id, open_date, status
- employee: id, name, position, start_date, retire_date, salary, (status)
- product: id, name, price, stock_shelf, stock_warehouse,
- transaction(giao dịch): id, employee_id, customer_id, datetime, payment_method, total_amount, discount, final_amount, (status)
- transaction_item: id, product_id, quantity, unit_price

POS Data

Loyalty:

- customer_id/phone: mã khách hàng
- name: tên
- join_date: ngày tham gia thành viên
- tier: hạng
- point: điểm tích lũy
- total_money_used: tổng tiền sử dụng

discount_percentage: % giảm giá dựa trên hạng 

Dataset:

- cửa hàng: thông tin từng cửa hàng
- khu vực
- sản phẩm: danh mục sản phẩm
- khách hàng: thông tin khách hàng
- nhân viên: thông tin nhân viên
- giao dịch: chi tiết từng đơn hàng
- kho hàng: thông tin kho
- thanh toán: thông tin thanh toán, loại hình, chiết khấu
- khuyến mãi: chương trình khuyến mãi, thời gian ưu đãi
