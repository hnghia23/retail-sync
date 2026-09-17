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
  (`CustomerSnapshot.anonymous()`). Thác đổ Redis + trung tâm là việc của tuần 2 ngày 9.
- **Mở ca qua route tạm** (`POST /ui/shifts/open`, INSERT trực tiếp) — route thật
  `POST /shifts/open` (FR-P11) là việc của tuần 2 ngày 10, sẽ THAY THẾ route tạm này.
- **Không CSRF token** — máy POS trong LAN nội bộ cửa hàng, không lộ ra internet. Cần bổ
  sung nếu UI này lộ ra ngoài LAN.
- **Giỏ hàng nằm ở server, đi vòng qua client bằng một hidden field JSON** — đúng triết lý
  HTMX (server là nguồn sự thật), không cần state JS phức tạp.
"""
