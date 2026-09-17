"""Xếp hạng vai trò — `edge/pos/domain/auth.py`."""

from __future__ import annotations

import pytest

from edge.pos.domain.auth import UnknownRoleError, meets_minimum


@pytest.mark.parametrize(
    "role,minimum,expected",
    [
        ("cashier", "cashier", True),
        ("manager", "cashier", True),
        ("cashier", "manager", False),
        ("admin", "manager", True),
        ("region_manager", "manager", True),
        ("cashier", "admin", False),
    ],
)
def test_meets_minimum(role: str, minimum: str, expected: bool) -> None:
    assert meets_minimum(role, minimum) == expected  # type: ignore[arg-type]


def test_unknown_role_is_rejected_not_defaulted() -> None:
    """Khác với hạng khách (mặc định 0%), vai trò lạ phải TỪ CHỐI — đây là phân quyền."""
    with pytest.raises(UnknownRoleError):
        meets_minimum("supervisor", "cashier")  # type: ignore[arg-type]


def test_unknown_minimum_is_rejected() -> None:
    with pytest.raises(UnknownRoleError):
        meets_minimum("cashier", "supervisor")  # type: ignore[arg-type]
