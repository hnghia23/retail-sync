"""Module Reporting — lớp truy vấn A (docs/03 §5b).

Đọc THẲNG Postgres cửa hàng, không đi qua warehouse. Hai lý do:
  - Chốt ca phải làm được khi mất mạng.
  - Quản lý cửa hàng cần số của *hôm nay*; pipeline T+1 về định nghĩa không phục vụ được.

Chỉ đọc. Ngoại lệ: mở/đóng ca ghi bảng `shift` (FR-P11) — đây là nghiệp vụ vận hành,
không phải giao dịch bán hàng.
"""
