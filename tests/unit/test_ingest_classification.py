"""Phân loại lỗi ở trung tâm — quyết định sự kiện được thử lại hay vào dead-letter.

Sai chỗ này theo một hướng là mất dữ liệu (vứt sự kiện đúng), theo hướng kia là kẹt hàng
đợi (thử lại mãi một sự kiện không bao giờ qua). Bảng đầy đủ ở docstring `central.ingest.service`.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import DBAPIError

from central.ingest.service import _classify_db_error, _describe


class _OrigError(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__("x")
        self.sqlstate = sqlstate


def _db_error(sqlstate: str | None) -> DBAPIError:
    return DBAPIError("stmt", {}, _OrigError(sqlstate))


@pytest.mark.parametrize(
    ("sqlstate", "retryable"),
    [
        ("23503", True),  # khóa ngoại: sự kiện phụ thuộc chưa tới
        ("23505", False),  # unique: va chạm thật, gửi lại không sửa được
        ("23514", False),  # check — gồm cả "no partition found for row"
        ("23502", False),  # not null
        ("22P02", False),  # sai kiểu dữ liệu
        ("40P01", True),  # deadlock — tạm thời
        ("40001", True),  # serialization failure — tạm thời
        (None, True),  # không rõ → thiên về giữ lại
    ],
)
def test_sqlstate_classification(sqlstate: str | None, retryable: bool) -> None:
    assert _classify_db_error(_db_error(sqlstate))[0] is retryable


def test_validation_summary_never_echoes_input_values() -> None:
    """Ràng buộc #10 — lý do lỗi đi vào log và `dead_letter_event.error`, không được mang dữ
    liệu khách. `str(ValidationError)` mặc định in cả `input_value`."""

    class M(BaseModel):
        phone_hash: int

    with pytest.raises(ValidationError) as info:
        M.model_validate({"phone_hash": "0901234567-bi-mat"})

    summary = _describe(info.value)
    assert "phone_hash" in summary
    assert "0901234567" not in summary
