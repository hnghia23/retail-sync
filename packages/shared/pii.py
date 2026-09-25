"""SĐT khách — chuẩn hóa rồi băm. Ràng buộc #10: số đọc được không rời khỏi hàm này.

## Vì sao chuẩn hóa TRƯỚC khi băm

`phone_hash` là thứ duy nhất nối hai bản ghi của cùng một người: tra khách giữa các cửa hàng
(docs/13 §4) và dò trùng C03 ở trung tâm đều so hash. Cùng một người gõ `0901 234 567` ở cửa
hàng A và `+84901234567` ở cửa hàng B mà ra hai hash khác nhau thì C03 không bao giờ phát
hiện được, và điểm của khách nằm vĩnh viễn ở hai tài khoản.

## Vì sao HMAC có khóa, không phải SHA-256 trần

Không gian SĐT Việt Nam chỉ cỡ 10⁹ số: SHA-256 trần bị dò ngược hết trong vài phút bằng
cách băm thử mọi số. HMAC với khóa bí mật chung toàn chuỗi thì không dò được nếu không có
khóa. Khóa phải GIỐNG NHAU ở mọi cửa hàng và trung tâm, vì hash phải so được với nhau.

⚠️ Đổi khóa = mọi `phone_hash` đã có trở thành vô nghĩa (không tra, không dò trùng được nữa).
Không có đường xoay khóa ở POC — chọn một lần, giữ bí mật như mật khẩu DB.
"""

from __future__ import annotations

import hashlib
import hmac
import re

__all__ = ["InvalidPhoneError", "hash_phone", "normalize_vn_phone"]

_SEPARATORS = re.compile(r"[\s.\-()]")


class InvalidPhoneError(ValueError):
    """SĐT không nhận ra được. Thông điệp KHÔNG chứa số khách gõ (có thể đi vào log)."""


def normalize_vn_phone(raw: str) -> str:
    """Đưa về dạng nội địa `0xxxxxxxxx` (10 chữ số, hoặc 11 với số bàn cũ có mã vùng).

    Chấp nhận: dấu cách/chấm/gạch/ngoặc, tiền tố `+84`, `84`, `0084`. Từ chối mọi thứ khác
    thay vì đoán: đoán sai một số là gắn điểm của người này vào tài khoản người khác.
    """
    digits = _SEPARATORS.sub("", raw.strip())
    if digits.startswith("+"):
        digits = digits[1:]
    if not digits.isdigit():
        raise InvalidPhoneError("SĐT chỉ được chứa chữ số, dấu cách, chấm, gạch, ngoặc và '+'")

    if digits.startswith("0084"):
        digits = "0" + digits[4:]
    elif digits.startswith("84") and len(digits) in (11, 12):
        digits = "0" + digits[2:]

    if not digits.startswith("0") or len(digits) not in (10, 11) or digits.startswith("00"):
        raise InvalidPhoneError("SĐT Việt Nam phải có 10 hoặc 11 chữ số, bắt đầu bằng 0 hoặc +84")
    return digits


def hash_phone(raw: str, *, key: str) -> str:
    """`phone_hash` = HMAC-SHA256(khóa chuỗi, SĐT đã chuẩn hóa), dạng hex."""
    if not key:
        raise ValueError("Thiếu PII_HASH_KEY — không băm SĐT bằng khóa rỗng")
    normalized = normalize_vn_phone(raw)
    return hmac.new(key.encode(), normalized.encode(), hashlib.sha256).hexdigest()
