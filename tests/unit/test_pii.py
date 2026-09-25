"""`shared.pii` — chuẩn hóa + băm SĐT (ràng buộc #10, case C03)."""

from __future__ import annotations

import pytest

from shared.pii import InvalidPhoneError, hash_phone, normalize_vn_phone

KEY = "test-key"


@pytest.mark.parametrize(
    "raw",
    [
        "0901234567",
        "090 123 4567",
        "0901.234.567",
        "0901-234-567",
        "(090) 1234567",
        "+84901234567",
        "+84 90 123 4567",
        "84901234567",
        "0084901234567",
        "  0901234567  ",
    ],
)
def test_every_common_spelling_of_one_number_gives_one_hash(raw: str) -> None:
    """C03 chỉ dò được trùng khi cùng một người ra cùng một hash, dù thu ngân gõ kiểu gì."""
    assert normalize_vn_phone(raw) == "0901234567"
    assert hash_phone(raw, key=KEY) == hash_phone("0901234567", key=KEY)


def test_old_landline_with_area_code_is_accepted() -> None:
    assert normalize_vn_phone("024 3825 1234") == "02438251234"


@pytest.mark.parametrize(
    "raw",
    ["", "abc", "090123456", "090123456789", "901234567", "001234567890", "0901234567x", "+"],
)
def test_unrecognisable_numbers_are_rejected_not_guessed(raw: str) -> None:
    """Đoán sai một số là gắn điểm của người này vào tài khoản người khác."""
    with pytest.raises(InvalidPhoneError):
        normalize_vn_phone(raw)


def test_error_message_never_echoes_the_number() -> None:
    """Thông điệp lỗi có thể đi vào log và response — không được chứa PII."""
    with pytest.raises(InvalidPhoneError) as exc:
        normalize_vn_phone("0901234567999")
    assert "0901234567" not in str(exc.value)


def test_hash_depends_on_the_key() -> None:
    """Không có khóa thì không dò ngược được: 10⁹ số VN băm trần chỉ mất vài phút để thử hết."""
    assert hash_phone("0901234567", key="a") != hash_phone("0901234567", key="b")
    assert "0901234567" not in hash_phone("0901234567", key="a")


def test_empty_key_is_refused() -> None:
    with pytest.raises(ValueError, match="PII_HASH_KEY"):
        hash_phone("0901234567", key="")
