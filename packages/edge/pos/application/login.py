"""Use case `Login` — FR-P01, FR-P02.

docs/13 §1: `POST /auth/login`, không cần vai trò tối thiểu (route mở), trả access token
(15 phút) + refresh token (7 ngày).

Bốn lý do từ chối, và cả bốn trả về CÙNG một thông báo lỗi (`INVALID_CREDENTIALS`):
sai mã nhân viên, sai mật khẩu, tài khoản `is_active=false`, tài khoản đã `revoked_at`.
Không phân biệt để kẻ tấn công không dò được "mã nhân viên này có tồn tại không" qua nội
dung lỗi.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from shared.security import encode_access_token, encode_refresh_token, verify_password

if TYPE_CHECKING:
    from shared.config import JwtSettings
    from shared.types import Role


class LoginRejectedError(Exception):
    """Sai thông tin đăng nhập hoặc tài khoản bị khóa — luôn dịch sang `401`."""


@dataclass(frozen=True, slots=True)
class EmployeeAccount:
    """Ảnh chụp một dòng `employee_cache` cần cho việc đăng nhập."""

    employee_id: str
    role: Role
    password_hash: str
    is_active: bool
    revoked_at: datetime | None


class EmployeeAuthPort(Protocol):
    async def find_by_id(self, employee_id: str) -> EmployeeAccount | None: ...


@dataclass(frozen=True, slots=True)
class LoginCommand:
    employee_id: str
    password: str


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    role: Role
    expires_in_minutes: int


async def login(
    command: LoginCommand,
    *,
    employees: EmployeeAuthPort,
    store_id: str,
    jwt_settings: JwtSettings,
) -> TokenPair:
    account = await employees.find_by_id(command.employee_id)

    # S01: thu hồi quyền phải có hiệu lực NGAY CẢ KHI cửa hàng offline — `employee_cache`
    # là bản sao cục bộ, không hỏi trung tâm ở bước này (docs/05 §3.1).
    if (
        account is None
        or not account.is_active
        or account.revoked_at is not None
        or not verify_password(command.password, account.password_hash)
    ):
        raise LoginRejectedError("Mã nhân viên hoặc mật khẩu không đúng")

    access = encode_access_token(
        store_id=store_id,
        employee_id=account.employee_id,
        role=account.role,
        settings=jwt_settings,
    )
    refresh = encode_refresh_token(
        store_id=store_id, employee_id=account.employee_id, settings=jwt_settings
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        role=account.role,
        expires_in_minutes=jwt_settings.access_token_ttl_minutes,
    )
