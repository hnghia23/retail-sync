"""Use case `Login` — FR-P01, FR-P02. Chạy bằng fake, không cần DB."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from edge.pos.application.login import (
    EmployeeAccount,
    LoginCommand,
    LoginRejectedError,
    login,
)
from shared.config import JwtSettings
from shared.security import decode_access_token, hash_password


def _jwt_settings() -> JwtSettings:
    return JwtSettings(_env_file=None, secret_key="test-secret")


@dataclass
class FakeEmployees:
    accounts: dict[str, EmployeeAccount] = field(default_factory=dict)

    async def find_by_id(self, employee_id: str) -> EmployeeAccount | None:
        return self.accounts.get(employee_id)


def _active_cashier(password: str = "dung-mat-khau") -> EmployeeAccount:
    return EmployeeAccount(
        employee_id="emp-007",
        role="cashier",
        password_hash=hash_password(password),
        is_active=True,
        revoked_at=None,
    )


async def test_correct_credentials_issue_tokens() -> None:
    employees = FakeEmployees({"emp-007": _active_cashier()})
    result = await login(
        LoginCommand("emp-007", "dung-mat-khau"),
        employees=employees,
        store_id="store-001",
        jwt_settings=_jwt_settings(),
    )
    assert result.role == "cashier"
    claims = decode_access_token(result.access_token, settings=_jwt_settings())
    assert claims.employee_id == "emp-007"
    assert claims.store_id == "store-001"


async def test_wrong_password_is_rejected() -> None:
    employees = FakeEmployees({"emp-007": _active_cashier()})
    with pytest.raises(LoginRejectedError):
        await login(
            LoginCommand("emp-007", "sai-mat-khau"),
            employees=employees,
            store_id="store-001",
            jwt_settings=_jwt_settings(),
        )


async def test_unknown_employee_is_rejected() -> None:
    employees = FakeEmployees({})
    with pytest.raises(LoginRejectedError):
        await login(
            LoginCommand("emp-999", "bat-ky-gi"),
            employees=employees,
            store_id="store-001",
            jwt_settings=_jwt_settings(),
        )


async def test_inactive_employee_is_rejected_even_with_correct_password() -> None:
    account = EmployeeAccount(
        employee_id="emp-007",
        role="cashier",
        password_hash=hash_password("dung"),
        is_active=False,
        revoked_at=None,
    )
    employees = FakeEmployees({"emp-007": account})
    with pytest.raises(LoginRejectedError):
        await login(
            LoginCommand("emp-007", "dung"),
            employees=employees,
            store_id="store-001",
            jwt_settings=_jwt_settings(),
        )


async def test_revoked_employee_is_rejected() -> None:
    """S01: thu hồi quyền có hiệu lực ngay cả khi cửa hàng offline (đọc bản cục bộ)."""
    account = EmployeeAccount(
        employee_id="emp-007",
        role="manager",
        password_hash=hash_password("dung"),
        is_active=True,
        revoked_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    employees = FakeEmployees({"emp-007": account})
    with pytest.raises(LoginRejectedError):
        await login(
            LoginCommand("emp-007", "dung"),
            employees=employees,
            store_id="store-001",
            jwt_settings=_jwt_settings(),
        )


async def test_error_message_does_not_distinguish_reasons() -> None:
    """Sai mã nhân viên và sai mật khẩu phải trả CÙNG một thông báo — không dò được tài
    khoản nào tồn tại."""
    employees = FakeEmployees({"emp-007": _active_cashier()})

    async def _fail(cmd: LoginCommand) -> str:
        with pytest.raises(LoginRejectedError) as exc:
            await login(
                cmd, employees=employees, store_id="store-001", jwt_settings=_jwt_settings()
            )
        return str(exc.value)

    wrong_password = await _fail(LoginCommand("emp-007", "sai"))
    unknown_employee = await _fail(LoginCommand("emp-999", "bat-ky"))
    assert wrong_password == unknown_employee


async def test_access_token_carries_role_refresh_token_does_not() -> None:
    employees = FakeEmployees({"emp-007": _active_cashier()})
    result = await login(
        LoginCommand("emp-007", "dung-mat-khau"),
        employees=employees,
        store_id="store-001",
        jwt_settings=_jwt_settings(),
    )
    import jwt as pyjwt

    access_payload = pyjwt.decode(result.access_token, "test-secret", algorithms=["HS256"])
    refresh_payload = pyjwt.decode(result.refresh_token, "test-secret", algorithms=["HS256"])
    assert access_payload["role"] == "cashier"
    assert "role" not in refresh_payload
