"""Băm mật khẩu + JWT — tiện ích hạ tầng thuần, không chứa nghiệp vụ.

Đặt ở `shared` vì đây là crypto tổng quát (giống `shared.tracing`, `shared.db`), không
thuộc riêng về `pos`: bất kỳ module nào cần xác thực đều gọi được mà không phải import
`edge.pos`.

Hai việc, và chỉ hai việc:
  - `hash_password` / `verify_password` — Argon2id qua `argon2-cffi`.
  - `encode_access_token` / `decode_access_token` — JWT qua `pyjwt`, thuật toán HS256.

Không có logic "ai được làm gì" ở đây — đó là việc của `edge.pos.domain.auth`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shared.config import JwtSettings
    from shared.types import Role


class InvalidTokenError(Exception):
    """Token hết hạn, sai chữ ký, hoặc thiếu claim bắt buộc."""


# ═══════════════════════ Mật khẩu ═══════════════════════


def hash_password(plain: str) -> str:
    """Argon2id với tham số mặc định của `argon2-cffi` (đã đủ mạnh cho 2026)."""
    from argon2 import PasswordHasher

    return PasswordHasher().hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """`False` cho MỌI lỗi — sai mật khẩu, hash hỏng, hay hash rỗng đều là "không khớp".

    Không phân biệt lý do trong thông báo lỗi: nếu không, kẻ tấn công dò được "tài khoản
    tồn tại nhưng sai mật khẩu" khác với "tài khoản không tồn tại" qua thời gian phản hồi
    hoặc loại lỗi.
    """
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError

    if not hashed:
        return False
    try:
        PasswordHasher().verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception:  # pragma: no cover — hash hỏng/định dạng lạ, coi như không khớp
        return False
    return True


# ═══════════════════════ JWT ═══════════════════════


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """Nội dung access token — docs/13 §1: "token mang `store_id` + `employee_id` + `role`"."""

    store_id: str
    employee_id: str
    role: Role
    issued_at: datetime
    expires_at: datetime


def encode_access_token(
    *,
    store_id: str,
    employee_id: str,
    role: Role,
    settings: JwtSettings,
    now: datetime | None = None,
) -> str:
    import jwt

    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.access_token_ttl_minutes)
    payload: dict[str, Any] = {
        "store_id": store_id,
        "employee_id": employee_id,
        "role": role,
        "iat": issued_at,
        "exp": expires_at,
        "jti": str(uuid.uuid4()),
        "type": "access",
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def encode_refresh_token(
    *, store_id: str, employee_id: str, settings: JwtSettings, now: datetime | None = None
) -> str:
    """Refresh token KHÔNG mang `role`.

    Vai trò có thể đổi trong 7 ngày hiệu lực của refresh token (thu ngân được lên chức
    quản lý); mang `role` cũ vào đây là mời gọi việc dùng nhầm token refresh như access
    token và cấp quyền theo vai trò đã lỗi thời.
    """
    import jwt

    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(days=settings.refresh_token_ttl_days)
    payload: dict[str, Any] = {
        "store_id": store_id,
        "employee_id": employee_id,
        "iat": issued_at,
        "exp": expires_at,
        "jti": str(uuid.uuid4()),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_access_token(token: str, *, settings: JwtSettings) -> AccessTokenClaims:
    """Giải mã + kiểm chữ ký + hạn dùng. Ném `InvalidTokenError` cho MỌI lý do thất bại.

    Không phân biệt "hết hạn" với "sai chữ ký" ra ngoài: cả hai đều là `401`, và không cần
    cho client biết chi tiết tại sao token của nó không hợp lệ.
    """
    import jwt

    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError("Token không hợp lệ hoặc đã hết hạn") from exc

    if payload.get("type") != "access":
        raise InvalidTokenError("Token không phải access token")

    try:
        return AccessTokenClaims(
            store_id=payload["store_id"],
            employee_id=payload["employee_id"],
            role=payload["role"],
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except KeyError as exc:
        raise InvalidTokenError(f"Token thiếu claim bắt buộc: {exc}") from exc
