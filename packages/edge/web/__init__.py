"""UI POS — HTMX + Alpine.js, render phía server (ADR-008).

Roadmap tuần 1 ngày 5: "đăng nhập → quét hàng → thanh toán → hóa đơn. Xấu cũng được, phải
chạy được." Đây là tầng trình bày mỏng gọi vào use case THẬT của `edge.pos`/`edge.loyalty`
— không có logic nghiệp vụ riêng ở đây, chỉ dịch HTML ↔ use case.

## Vì sao vendor htmx.js/alpine.js cục bộ, không tải từ CDN

Nguyên tắc kiến trúc #1: "cửa hàng phải bán được hàng khi mất mạng hoàn toàn." Nếu UI tải
JS từ CDN, cửa hàng mất mạng thì trang trắng — vi phạm chính nguyên tắc cứng nhất của dự
án. `static/vendor/{htmx,alpine}.min.js` được tải về và phục vụ từ chính edge-api.

## Đơn giản hóa có chủ đích (ghi rõ để không ai tưởng nhầm là đã đủ)

- **Không tra cứu khách hàng** — mọi đơn qua UI đều là khách vãng lai
  (`CustomerSnapshot.anonymous()`). Tra khách là việc của giai đoạn C (ADR-010): UI đóng
  băng, dữ liệu khách của giai đoạn A/B đi qua `POST /api/v1/customers` từ bộ giả lập.
- **Mở ca** (`POST /ui/shifts/open`) gọi đúng use case `open_shift()` của API, với
  `business_date` = ngày theo giờ cửa hàng. Chưa có màn hình đóng ca — đóng ca qua
  `POST /api/v1/shifts/{id}/close`.
- **Không CSRF token** — máy POS trong LAN nội bộ cửa hàng, không lộ ra internet. Cần bổ
  sung nếu UI này lộ ra ngoài LAN.
- **Giỏ hàng nằm ở server, đi vòng qua client bằng một hidden field JSON** — đúng triết lý
  HTMX (server là nguồn sự thật), không cần state JS phức tạp.
"""
