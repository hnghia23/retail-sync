# 01 — Yêu cầu

Ký hiệu ưu tiên theo MoSCoW: **[M]** Must (v1 bắt buộc) · **[S]** Should (v1 nếu kịp) ·
**[C]** Could (v2) · **[W]** Không làm

---

## 1. Yêu cầu chức năng — POS

| ID | Yêu cầu | Ưu tiên | Ghi chú |
|---|---|---|---|
| FR-P01 | Nhân viên đăng nhập bằng tài khoản, phiên làm việc gắn với cửa hàng | **M** | Token phải mang `store_id` — quyết định mọi phân quyền sau đó |
| FR-P02 | Phân quyền theo vai trò: thu ngân / quản lý cửa hàng / quản trị | **M** | Thu ngân không được hủy đơn đã chốt, không được sửa giá |
| FR-P03 | Tra cứu sản phẩm theo mã hoặc mã vạch → tên, đơn giá, tồn kho | **M** | p95 < 100ms, phải chạy được khi offline |
| FR-P04 | Tạo giỏ hàng nhiều dòng: thêm, sửa số lượng, xóa dòng | **M** | |
| FR-P05 | Tính tiền: tạm tính → chiết khấu → thành tiền | **M** | Chiết khấu từ 2 nguồn: hạng khách + khuyến mãi. Quy tắc cộng gộp phải rõ |
| FR-P06 | Thanh toán đa phương thức: tiền mặt / thẻ / ví điện tử | **M** | v1 chỉ **ghi nhận** phương thức, không tích hợp cổng thanh toán |
| FR-P07 | Lưu đơn vào DB cửa hàng, trả hóa đơn cho UI | **M** | Ghi thành công cục bộ = giao dịch hoàn tất, kể cả khi mất mạng |
| FR-P08 | Bán hàng bình thường khi mất kết nối trung tâm | **M** | Ràng buộc cứng. Xem NFR-01 |
| FR-P09 | Trả hàng / hủy đơn, sinh giao dịch đối ứng | **M** | Phải hoàn/trừ điểm tương ứng (FR-L08). Không xóa đơn gốc |
| FR-P10 | Trừ tồn kho quầy khi bán, cộng lại khi trả | **S** | |
| FR-P11 | **Chốt ca**: mở/đóng ca, tổng doanh thu theo ca | **M** ⬆️ | **Nâng từ S lên M** — nghiệp vụ **hằng ngày bắt buộc** của mọi cửa hàng ([B01 §2](business/01-operating-model.md)). *Phần đối chiếu tiền mặt vẫn là Could* |
| FR-P12 | In hóa đơn ra máy in nhiệt | **C** | v1 hiển thị HTML là đủ |
| FR-P13 | Bán hàng có đặt cọc, trả góp | **W** | |
| **FR-P14** 🆕 | **Thanh toán tách: một đơn, nhiều hình thức** *(500K tiền mặt + 300K thẻ)* | **M** | Rất phổ biến ở bán lẻ. Cần bảng `sale_payment` ([B02 P01](business/02-edge-cases.md)) |
| **FR-P15** 🆕 | **Trả hàng một phần** *(2 trong 5 sản phẩm)* | **M** | Cần `original_sale_line_no`, nếu không sẽ trả quá số đã mua ([B02 R02](business/02-edge-cases.md)) |
| **FR-P16** 🆕 | Ghi nhận tiền khách đưa và tiền thối | **M** | Không suy ra được từ `total` |
| **FR-P17** 🆕 | Tra cứu đơn cũ theo SĐT khách để trả hàng *(không có hóa đơn giấy)* | **M** | Thực tế khách hiếm khi giữ hóa đơn ([B02 R03](business/02-edge-cases.md)) |

## 2. Yêu cầu chức năng — Loyalty

| ID | Yêu cầu | Ưu tiên | Ghi chú |
|---|---|---|---|
| FR-L01 | Nhận diện khách bằng số điện thoại | **M** | SĐT là PII → xem NFR-06 |
| FR-L02 | Đăng ký khách mới ngay tại quầy | **M** | Phải hoạt động cả khi offline (tạo cục bộ, đồng bộ sau) |
| FR-L03 | Tính điểm từ giá trị đơn theo quy tắc **cấu hình được** | **M** | Quy tắc nằm trong dữ liệu, không hardcode. Mặc định: 10.000đ = 1 điểm |
| FR-L04 | Xếp hạng tự động theo điểm tích lũy | **M** | Bronze/Silver/Gold/Platinum — ngưỡng cấu hình được |
| FR-L05 | Áp dụng % giảm giá theo hạng khi thanh toán | **M** | Hạng đọc lúc bắt đầu đơn, không đổi giữa chừng |
| FR-L06 | **Điểm đúng khi khách mua ở nhiều cửa hàng** | **M** | Yêu cầu khó nhất của toàn hệ thống. Xem [ADR-002](adr/002-point-ledger.md) |
| FR-L07 | Tra cứu lịch sử biến động điểm của khách | **M** | Bắt buộc để xử lý khiếu nại |
| FR-L08 | Hoàn / trừ điểm khi trả hàng | **M** | |
| FR-L09 | Điểm không bao giờ cộng hai lần dù đồng bộ lặp | **M** | Idempotency theo `event_id` |
| FR-L10 | Dùng điểm để đổi ưu đãi (redeem) | **S** | Cần cơ chế khóa để tránh tiêu âm điểm |
| FR-L11 | Điểm hết hạn sau N tháng | **C** | Loyalty thật đều có. Ledger đã hỗ trợ sẵn (dòng delta âm) |
| FR-L12 | Chiến dịch điểm thưởng (x2 điểm cuối tuần...) | **C** | |
| FR-L13 | Gộp tài khoản khách trùng | **C** | Ledger làm việc này dễ: đổi `customer_id` các dòng |

## 2b. Yêu cầu chức năng — Báo cáo vận hành 🆕

Lớp truy vấn có **tần suất cao nhất** nhưng thiết kế ban đầu bỏ sót hoàn toàn. Đọc thẳng
Postgres, **không đi qua warehouse** ([B03](business/03-analytical-workload.md)).

| ID | Yêu cầu | Ưu tiên | Ghi chú |
|---|---|---|---|
| FR-R01 | Báo cáo chốt ca: doanh thu, số đơn, phân rã theo hình thức thanh toán | **M** | T+0, chạy được khi offline. Quét < 1 000 dòng |
| FR-R02 | Doanh thu hôm nay của cửa hàng này | **M** | T+0, ~10 lần/cửa hàng/ngày |
| FR-R03 | Doanh thu theo thu ngân trong ca | **M** | T+0 |
| FR-R04 | Báo cáo 30 ngày của một cửa hàng | **S** | T+0 → T+1, vẫn chạy trên Postgres |
| FR-R05 | So sánh các cửa hàng trong khu vực | **S** | Postgres trung tâm |
| FR-R06 | Đối chiếu tiền mặt khi chốt ca *(variance)* | **C** | Nghiệp vụ thật cần, nhưng không chặn |

## 3. Yêu cầu chức năng — Trung tâm & Dữ liệu

| ID | Yêu cầu | Ưu tiên | Ghi chú |
|---|---|---|---|
| FR-C01 | Đồng bộ master data xuống cửa hàng: sản phẩm, giá, khuyến mãi, nhân viên, quy tắc hạng | **M** | Có phiên bản, tải tiếp được sau khi đứt |
| FR-C02 | Thu thập giao dịch từ mọi cửa hàng về trung tâm | **M** | At-least-once + idempotent = hiệu quả exactly-once |
| FR-C03 | Khách hàng master ở trung tâm, tra cứu được từ bất kỳ cửa hàng nào | **M** | |
| FR-C04 | Lake lưu dữ liệu thô, bất biến, có phân vùng | **M** | Là lưới an toàn: dựng lại warehouse từ đầu được |
| FR-C05 | Warehouse mô hình chiều (star schema) | **M** | |
| FR-C06 | Pipeline **idempotent** — chạy lại không nhân đôi dữ liệu | **M** | Lỗi nghiêm trọng nhất của v1 |
| FR-C07 | Kiểm tra chất lượng dữ liệu tự động trong pipeline | **M** | not_null, unique, quan hệ khóa, giá trị hợp lệ |
| FR-C08 | Dashboard: doanh thu, sản phẩm, khách hàng, hiệu quả loyalty | **M** | |
| FR-C09 | Nạp lại (backfill) dữ liệu theo khoảng ngày | **S** | |
| FR-C10 | Theo dõi tình trạng đồng bộ của từng cửa hàng | **S** | Cửa hàng nào đang trễ, trễ bao lâu |
| FR-C11 | Theo dõi thay đổi chậm (SCD Type 2) cho khách hàng và giá sản phẩm | **S** | Để phân tích được "khách lên hạng lúc nào" |
| FR-C12 | Warehouse cập nhật gần thời gian thực | **C** | T+1 đủ cho v1 |

---

## 4. Yêu cầu phi chức năng

### NFR-01 — Khả dụng *(ràng buộc cứng)*

| Kịch bản | Hành vi bắt buộc |
|---|---|
| Mất kết nối trung tâm | Bán hàng **bình thường**. Khách đã biết → tích điểm cục bộ. Khách lạ → vẫn bán, ghi nhận định danh, xử lý điểm khi có mạng |
| Redis chết | POS chạy tiếp, chỉ chậm hơn. **Cache không bao giờ là thành phần sống còn** |
| DB trung tâm chết | Cửa hàng không bị ảnh hưởng. Đồng bộ dồn lại trong outbox, tự chảy khi phục hồi |
| DB cửa hàng chết | Cửa hàng đó dừng. Chấp nhận được — đây là ranh giới chịu lỗi |

Mục tiêu: **100% khả năng bán hàng khi offline**, loyalty suy giảm có kiểm soát.

### NFR-02 — Hiệu năng

| Thao tác | Mục tiêu p95 | Mục tiêu p99 |
|---|---|---|
| Tra giá sản phẩm | < 100 ms | < 250 ms |
| Tra cứu khách (cache hit) | < 20 ms | < 50 ms |
| Tra cứu khách (cache miss, DB cục bộ) | < 100 ms | < 200 ms |
| Tra cứu khách (gọi trung tâm) | < 500 ms | < 2 s *(timeout rồi bỏ qua)* |
| Tạo đơn hoàn tất | < 500 ms | < 1 s |
| Truy vấn dashboard | < 3 s | < 10 s |

Lưu ý: gọi trung tâm **không bao giờ được chặn** việc chốt đơn. Quá timeout → bán tiếp,
xử lý điểm bất đồng bộ.

### NFR-03 — Độ bền

- Không được mất giao dịch đã ghi thành công tại cửa hàng.
- Mọi sự kiện đồng bộ ít nhất một lần, kèm khóa idempotency → **hiệu quả exactly-once**.
- Dữ liệu thô trong lake là bất biến. Warehouse luôn dựng lại được từ lake.
- RPO cửa hàng ≤ 24h (backup ngày). RPO trung tâm ≤ 1h.

### NFR-04 — Tính nhất quán

| Dữ liệu | Mô hình | Lý do |
|---|---|---|
| Đơn hàng tại cửa hàng | Strong (ACID cục bộ) | Tiền thật, phải đúng ngay |
| Tồn kho quầy | Strong cục bộ | |
| Số dư điểm | **Eventual**, hội tụ < 5 phút khi mạng bình thường | Không thể strong nếu muốn offline. Xem [ADR-002](adr/002-point-ledger.md) |
| Master data (giá, khuyến mãi) | Eventual, trễ < 15 phút | |
| Warehouse | T+1 | |

**Đánh đổi đã chấp nhận rõ ràng:** khách mua ở hai cửa hàng trong vài phút có thể thấy số
dư điểm tạm thời cũ. Điểm **không bao giờ mất** — mọi dòng ledger cuối cùng đều được cộng.
Đây là đánh đổi đúng: mất điểm là không chấp nhận được, thấy trễ thì chấp nhận được.

### NFR-05 — Tính đúng đắn & kiểm toán

- Mọi biến động điểm có một dòng ledger bất biến kèm: nguyên nhân, giao dịch nguồn, cửa
  hàng, thời điểm xảy ra, thời điểm ghi nhận.
- `số dư = SUM(ledger.delta)` — bất biến này phải kiểm chứng được bằng một câu truy vấn.
- Job đối soát hằng ngày: so số dư vật chất hóa với tổng ledger, cảnh báo nếu lệch.

### NFR-06 — Bảo mật

| Hạng mục | Yêu cầu |
|---|---|
| Xác thực | JWT access ~15 phút + refresh token. Mật khẩu băm Argon2id |
| Phân quyền | Theo vai trò, phạm vi theo cửa hàng. Token cửa hàng A không đọc được dữ liệu cửa hàng B |
| PII | Tên và SĐT khách là PII. Mã hóa at-rest, **mask trong warehouse và dashboard** |
| Bí mật | Không hardcode. Biến môi trường ở local, secret manager ở production |
| Kênh truyền | TLS bắt buộc giữa cửa hàng và trung tâm ở production |
| Nhật ký truy cập | Ghi lại mọi lần đọc PII |
| Không lưu | Số thẻ, CVV — không bao giờ chạm tới (tránh phạm vi PCI-DSS) |

### NFR-07 — Khả năng vận hành

- Toàn hệ thống khởi động bằng `docker compose up` trên **một máy 16GB**.
- Chạy được từng phần: chỉ edge, chỉ central, chỉ data platform.
- Log JSON có cấu trúc, có `trace_id` xuyên suốt cửa hàng → trung tâm.
- Healthcheck cho mọi service.
- Một người phải vận hành được. **Mỗi thành phần hạ tầng thêm vào phải tự biện minh.**

> 🎯 **Cập nhật 2026-09-11:** chủ dự án xác nhận **NFR-01 (khả dụng), NFR-03 (độ bền),
> NFR-07 (vận hành) và NFR-08 (mở rộng) là ưu tiên cao nhất**, đứng trên các FR ở mục 1–3.
> Yêu cầu chi tiết và kế hoạch chứng minh: [08-reliability-and-scale.md](08-reliability-and-scale.md).

### NFR-08 — Khả năng mở rộng *(scale seams)*

Kiến trúc không được chặn đường từ 20 → 2000 cửa hàng. Với mỗi thành phần phải trả lời
được: *"khi hết dư địa thì đổi sang cái gì, và tốn bao nhiêu?"* — ghi trong
[04-tech-stack.md](04-tech-stack.md).

Quy tắc: **không dùng công nghệ phân tán khi một node còn dư trên 10 lần.**
Xem [02-scale-capacity.md](02-scale-capacity.md) để biết dư địa thật là bao nhiêu.

### NFR-09 — Chi phí

- POC: 0đ, chạy local.
- Production T1 (20 cửa hàng): mục tiêu < 100 USD/tháng hạ tầng trung tâm.
- Mỗi cửa hàng: chạy được trên mini PC ~8GB RAM.

### NFR-10 — Khả năng kiểm thử

- Logic nghiệp vụ (tính điểm, xếp hạng, tính tiền) test được **không cần hạ tầng**.
- Test tích hợp dùng DB thật qua testcontainers, không dùng mock.
- Mô phỏng được: nhiều cửa hàng, mất mạng, đồng bộ lặp, mua đồng thời hai nơi.

---

## 5. Kịch bản nghiệm thu

Đây là thước đo "xong hay chưa". Mỗi kịch bản phải là một test tự động.

| # | Kịch bản | Kết quả mong đợi |
|---|---|---|
| AT-01 | Bán đơn thường có nhận diện khách | Đơn được lưu, điểm tăng đúng công thức |
| AT-02 | **Ngắt mạng trung tâm, bán 10 đơn, nối lại mạng** | 10 đơn đều lên trung tâm, điểm cộng đúng một lần |
| AT-03 | **Gửi lặp cùng một sự kiện đồng bộ 5 lần** | Điểm chỉ cộng một lần |
| AT-04 | **Cùng một khách mua đồng thời ở cửa hàng 1 và 2** | Số dư cuối = tổng cả hai, không mất dòng nào |
| AT-05 | Khách mới ở cửa hàng 2, đã có điểm từ cửa hàng 1 | Cửa hàng 2 lấy đúng số dư từ trung tâm |
| AT-06 | Trả hàng một đơn đã tích điểm | Điểm bị trừ đúng, ledger có dòng đối ứng |
| AT-07 | **Chạy pipeline dữ liệu hai lần liên tiếp** | Số dòng trong warehouse không đổi |
| AT-08 | Tắt Redis rồi bán hàng | Bán bình thường, chỉ chậm hơn |
| AT-09 | Khách lên đủ ngưỡng điểm | Hạng tự nâng, đơn kế tiếp được giảm giá đúng % |
| AT-10 | Đối soát: tổng ledger vs số dư vật chất hóa | Khớp tuyệt đối |

---
*Changelog: 2026-09-11 — tạo mới.*
