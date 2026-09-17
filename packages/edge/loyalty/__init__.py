"""Module Loyalty — điểm tích lũy, hạng khách (phía cửa hàng).

Hai ràng buộc không được quên:
  - Ledger **append-only** (ADR-002). Không bao giờ `UPDATE` số dư.
  - Xếp hạng dùng `lifetime_earned`, KHÔNG phải `balance` — tiêu điểm không làm tụt hạng.

`loyalty` KHÔNG BAO GIỜ import `pos` (một chiều, cưỡng chế bởi import-linter).

Chiều ngược lại đi qua đúng một cửa: `edge/loyalty/api.py` — nơi sẽ đặt implementation của
`edge.pos.application.ports.LoyaltyPort`. Đó cũng là **đường cắt microservice tương lai**:
khi cần tách, chỉ đổi thân các hàm ở đó từ gọi hàm sang gọi HTTP (ADR-004).
"""
